#!/usr/bin/env python3
"""
seer_bgm_gui.py
===============
Tkinter desktop UI for the Seer BGM extractor.

Designed to be packaged as a single Windows .exe via PyInstaller — see
build.bat. Tkinter is part of the standard library, so the only extra
runtime deps are the same as the CLI (UnityPy, albi0, fsb5).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import locale
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from tkinter import (
    Tk, StringVar, BooleanVar, END, DISABLED, NORMAL, W,
    filedialog, messagebox,
)
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

# Core extraction logic (sits next to this file)
from extract_seer_bgm import (
    download_bundles,
    extract_from_bundle,
    hash_existing_outputs,
    iter_bundle_files,
)


# ---------------------------------------------------------------------------
# Stdout/stderr -> Tk log widget plumbing
# ---------------------------------------------------------------------------

log_queue: "queue.Queue[str | None]" = queue.Queue()


class TextRedirector(io.TextIOBase):
    """
    Captures writes from stdout/stderr and feeds them into a queue the GUI
    main thread polls. Handles tqdm-style \\r progress lines (each \\r
    resets the current line buffer, so the log doesn't get spammed with
    every progress-bar tick).
    """

    def __init__(self, q: "queue.Queue[str | None]") -> None:
        self._q = q
        self._line = ""

    def write(self, s) -> int:
        if not s:
            return 0
        # click.echo (used by albi0 for its log lines) writes bytes when the
        # destination stream isn't a TTY. Iterating over bytes yields ints,
        # which would explode the loop below — decode first.
        if isinstance(s, (bytes, bytearray, memoryview)):
            s = bytes(s).decode("utf-8", errors="replace")
        elif not isinstance(s, str):
            s = str(s)
        for ch in s:
            if ch == "\r":
                self._line = ""  # tqdm: overwrite current line
            elif ch == "\n":
                self._q.put((self._line + "\n") if self._line else "\n")
                self._line = ""
            else:
                self._line += ch
        return len(s)

    def flush(self) -> None:
        if self._line:
            self._q.put(self._line + "\n")
            self._line = ""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: Tk) -> None:
        self.root = root

        # --- Language: load saved choice, else auto-detect from locale ---
        cfg = _load_config()
        lang = cfg.get("language") or _detect_default_language()
        self._lang = lang if lang in TRANSLATIONS else "en"
        # Each entry: (widget, key, attr, format_kwargs)
        self._translatables: list[tuple] = []
        self._status_state = "idle"

        root.title(self.tr("window_title"))
        # Scale geometry by the system DPI so the window keeps the same
        # physical size after declaring DPI awareness. Without this, the
        # bottom of the window (the log area) gets clipped on HiDPI displays.
        scale = _dpi_scale()
        root.geometry(f"{int(760 * scale)}x{int(680 * scale)}")
        root.minsize(int(660 * scale), int(560 * scale))

        # Custom window icon (top-left + taskbar). On Windows, iconbitmap
        # accepts .ico and applies to both. Fail silently if the icon
        # isn't found — the app should still launch with the default Tk icon.
        icon_path = bundled_resource("app_icon.ico")
        if icon_path is not None:
            try:
                root.iconbitmap(default=str(icon_path))
            except Exception:
                pass

        # Defaults anchor to the app's directory (next to the exe when
        # frozen), not the cwd — keeps the tool portable.
        anchor = app_anchor_dir()

        # State
        self.mode_var = StringVar(value="download")
        self.workdir_var = StringVar(value=str(anchor / "seer_workspace"))
        self.updater_var = StringVar(value="newseer.default")
        self.patterns_var = StringVar(value="*audio*, *sound*, *music*")
        self.local_var = StringVar()
        self.output_var = StringVar(value=str(anchor / "bgm_out"))
        self.all_audio_var = BooleanVar(value=False)
        self.scene_tag_var = BooleanVar(value=True)
        self.verbose_var = BooleanVar(value=False)

        self._worker: threading.Thread | None = None

        self._build_ui()
        self.root.after(100, self._drain_log_queue)

    # ---- UI construction -------------------------------------------------
    def _build_ui(self) -> None:
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=12, pady=12)
        main.columnconfigure(0, weight=1)
        # Log frame (last row) gets the spare vertical space.
        main.rowconfigure(5, weight=1)

        # --- Language strip (row 0) -----------------------------------
        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(0, weight=1)

        self.lang_combo = ttk.Combobox(
            top, values=list(LANG_DISPLAY.values()),
            state="readonly", width=10,
        )
        self.lang_combo.set(LANG_DISPLAY[self._lang])
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_lang_changed)
        self.lang_combo.pack(side="right")

        lang_lbl = ttk.Label(top)
        self._add_tr(lang_lbl, "language_label")
        lang_lbl.pack(side="right", padx=(0, 6))

        # --- Source frame (row 1) -------------------------------------
        src = ttk.LabelFrame(main)
        self._add_tr(src, "source_label")
        src.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        src.columnconfigure(1, weight=1)

        rb_dl = ttk.Radiobutton(
            src,
            variable=self.mode_var, value="download",
            command=self._update_mode_state,
        )
        self._add_tr(rb_dl, "download_radio")
        rb_dl.grid(row=0, column=0, columnspan=3, sticky=W, padx=8, pady=(8, 0))

        self.dl_widgets = []

        lbl = ttk.Label(src); self._add_tr(lbl, "working_dir_label")
        lbl.grid(row=1, column=0, sticky=W, padx=24, pady=2)
        e = ttk.Entry(src, textvariable=self.workdir_var)
        e.grid(row=1, column=1, sticky="ew", padx=4)
        b = ttk.Button(src, command=lambda: self._pick_dir(self.workdir_var))
        self._add_tr(b, "browse_button")
        b.grid(row=1, column=2, padx=4)
        self.dl_widgets += [e, b]

        lbl = ttk.Label(src); self._add_tr(lbl, "updater_name_label")
        lbl.grid(row=2, column=0, sticky=W, padx=24, pady=2)
        e = ttk.Entry(src, textvariable=self.updater_var)
        e.grid(row=2, column=1, columnspan=2, sticky="ew", padx=4)
        self.dl_widgets.append(e)

        hint = ttk.Label(src, foreground="gray")
        self._add_tr(hint, "updater_hint")
        hint.grid(row=3, column=1, columnspan=2, sticky=W, padx=4)
        self.dl_widgets.append(hint)

        lbl = ttk.Label(src); self._add_tr(lbl, "patterns_label")
        lbl.grid(row=4, column=0, sticky=W, padx=24, pady=2)
        e = ttk.Entry(src, textvariable=self.patterns_var)
        e.grid(row=4, column=1, columnspan=2, sticky="ew", padx=4)
        self.dl_widgets.append(e)

        hint = ttk.Label(src, foreground="gray")
        self._add_tr(hint, "patterns_hint")
        hint.grid(row=5, column=1, columnspan=2, sticky=W, padx=4, pady=(0, 4))
        self.dl_widgets.append(hint)

        rb_loc = ttk.Radiobutton(
            src,
            variable=self.mode_var, value="local",
            command=self._update_mode_state,
        )
        self._add_tr(rb_loc, "use_local_radio")
        rb_loc.grid(row=6, column=0, columnspan=3, sticky=W, padx=8, pady=(8, 0))

        self.local_widgets = []
        lbl = ttk.Label(src); self._add_tr(lbl, "path_label")
        lbl.grid(row=7, column=0, sticky=W, padx=24, pady=(2, 8))
        e = ttk.Entry(src, textvariable=self.local_var)
        e.grid(row=7, column=1, sticky="ew", padx=4, pady=(2, 8))
        b = ttk.Button(src, command=lambda: self._pick_dir(self.local_var))
        self._add_tr(b, "browse_button")
        b.grid(row=7, column=2, padx=4, pady=(2, 8))
        self.local_widgets += [e, b]

        # --- Output frame (row 2) -------------------------------------
        out = ttk.LabelFrame(main)
        self._add_tr(out, "output_label")
        out.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        out.columnconfigure(1, weight=1)

        lbl = ttk.Label(out); self._add_tr(lbl, "folder_label")
        lbl.grid(row=0, column=0, sticky=W, padx=8, pady=8)
        ttk.Entry(out, textvariable=self.output_var).grid(
            row=0, column=1, sticky="ew", padx=4, pady=8)
        b = ttk.Button(out, command=lambda: self._pick_dir(self.output_var))
        self._add_tr(b, "browse_button")
        b.grid(row=0, column=2, padx=4, pady=8)

        # --- Options frame (row 3) ------------------------------------
        opt = ttk.LabelFrame(main)
        self._add_tr(opt, "options_label")
        opt.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        cb = ttk.Checkbutton(opt, variable=self.all_audio_var)
        self._add_tr(cb, "all_audio_check")
        cb.grid(row=0, column=0, sticky=W, padx=8, pady=4)

        cb = ttk.Checkbutton(opt, variable=self.verbose_var)
        self._add_tr(cb, "verbose_check")
        cb.grid(row=0, column=1, sticky=W, padx=8, pady=4)

        cb = ttk.Checkbutton(opt, variable=self.scene_tag_var)
        self._add_tr(cb, "scene_tag_check")
        cb.grid(row=1, column=0, columnspan=2, sticky=W, padx=8, pady=(0, 4))

        # --- Actions (row 4) -----------------------------------------
        act = ttk.Frame(main)
        act.grid(row=4, column=0, sticky="ew", pady=(0, 8))

        self.run_btn = ttk.Button(act, command=self._on_start)
        self._add_tr(self.run_btn, "start_button")
        self.run_btn.pack(side="left")

        b = ttk.Button(act, command=self._open_output)
        self._add_tr(b, "open_output_button")
        b.pack(side="left", padx=8)

        # The status label's text is driven by self._status_state, not by
        # the static translatables list — see _set_status / _apply_language.
        self.status = ttk.Label(act, foreground="gray")
        self.status.pack(side="left", padx=12)
        self._set_status("idle")

        # --- Log frame (row 5) ---------------------------------------
        log_frame = ttk.LabelFrame(main)
        self._add_tr(log_frame, "log_label")
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = ScrolledText(log_frame, height=12, wrap="word",
                                font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.log.configure(state=DISABLED)

        self._update_mode_state()

    # ---- Translation helpers --------------------------------------------
    def tr(self, key: str, **fmt) -> str:
        """
        Look up a translated string. Falls back to English then to the key
        itself, so an untranslated key still produces *something* visible.
        """
        lang_dict = TRANSLATIONS.get(self._lang, TRANSLATIONS["en"])
        template = lang_dict.get(key) or TRANSLATIONS["en"].get(key) or key
        if fmt:
            try:
                return template.format(**fmt)
            except Exception:
                return template
        return template

    def _add_tr(self, widget, key: str, attr: str = "text", **fmt) -> None:
        """
        Register a widget for re-translation when the language changes,
        and set its initial text. Widgets registered here all use the
        same configure(attr=...) interface — works for Label, Button,
        Radiobutton, Checkbutton, LabelFrame, etc.
        """
        self._translatables.append((widget, key, attr, fmt))
        try:
            widget.configure(**{attr: self.tr(key, **fmt)})
        except Exception:
            pass

    def _apply_language(self) -> None:
        """Re-translate every registered widget plus the status & title."""
        self.root.title(self.tr("window_title"))
        for widget, key, attr, fmt in self._translatables:
            try:
                widget.configure(**{attr: self.tr(key, **fmt)})
            except Exception:
                pass
        # Status label isn't in _translatables because its key depends on
        # current state, not a fixed value. Re-apply it explicitly.
        self._set_status(self._status_state)

    def _on_lang_changed(self, _event=None) -> None:
        """Combobox selection handler. Updates UI and persists choice."""
        choice = self.lang_combo.get()
        new_lang = DISPLAY_TO_LANG.get(choice, "en")
        if new_lang == self._lang:
            return
        self._lang = new_lang
        self._apply_language()
        cfg = _load_config()
        cfg["language"] = new_lang
        _save_config(cfg)

    def _set_status(self, state: str) -> None:
        """
        Update the status label. `state` is one of 'idle', 'working',
        'done'; remembered so it can be re-translated when the user
        switches languages mid-run.
        """
        self._status_state = state
        colors = {"idle": "gray", "working": "blue", "done": "green"}
        try:
            self.status.configure(
                text=self.tr(f"status_{state}"),
                foreground=colors.get(state, "black"),
            )
        except Exception:
            pass

    # ---- UI behaviour ----------------------------------------------------
    def _update_mode_state(self) -> None:
        mode = self.mode_var.get()
        dl = NORMAL if mode == "download" else DISABLED
        loc = NORMAL if mode == "local" else DISABLED
        for w in self.dl_widgets:
            w.configure(state=dl)
        for w in self.local_widgets:
            w.configure(state=loc)

    def _pick_dir(self, var: StringVar) -> None:
        path = filedialog.askdirectory(initialdir=var.get() or str(Path.cwd()))
        if path:
            var.set(path)

    def _open_output(self) -> None:
        p = Path(self.output_var.get())
        if not p.exists():
            messagebox.showinfo(self.tr("open_folder_title"),
                                self.tr("open_folder_missing", path=p))
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess; subprocess.run(["open", str(p)])
            else:
                import subprocess; subprocess.run(["xdg-open", str(p)])
        except Exception as exc:
            messagebox.showerror(self.tr("open_folder_title"), str(exc))

    def _append_log(self, text: str) -> None:
        self.log.configure(state=NORMAL)
        self.log.insert(END, text)
        self.log.see(END)
        self.log.configure(state=DISABLED)

    def _drain_log_queue(self) -> None:
        try:
            while True:
                item = log_queue.get_nowait()
                if item is None:
                    self.run_btn.configure(state=NORMAL)
                    self._set_status("done")
                else:
                    self._append_log(item)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    # ---- Run extraction --------------------------------------------------
    def _on_start(self) -> None:
        if self._worker and self._worker.is_alive():
            return

        # Clear log
        self.log.configure(state=NORMAL)
        self.log.delete("1.0", END)
        self.log.configure(state=DISABLED)

        # Gather params from UI
        mode = self.mode_var.get()
        output_dir = Path(self.output_var.get()).expanduser()
        if not str(output_dir):
            messagebox.showerror(self.tr("missing_output_title"),
                                 self.tr("missing_output_msg"))
            return

        params: dict = {
            "mode": mode,
            "output_dir": output_dir,
            "all_audio": self.all_audio_var.get(),
            "verbose": self.verbose_var.get(),
            "tag_with_scene": self.scene_tag_var.get(),
        }

        if mode == "download":
            params["workdir"] = Path(self.workdir_var.get()).expanduser()
            params["updater"] = self.updater_var.get().strip() or "newseer.default"
            params["patterns"] = [p.strip() for p in self.patterns_var.get().split(",") if p.strip()]
            params["input_dir"] = None
        else:
            local = self.local_var.get().strip()
            if not local:
                messagebox.showerror(self.tr("missing_input_title"),
                                     self.tr("missing_input_msg"))
                return
            input_dir = Path(local).expanduser()
            if not input_dir.exists():
                messagebox.showerror(self.tr("bad_input_title"),
                                     self.tr("bad_input_msg", path=input_dir))
                return
            params["input_dir"] = input_dir

        self.run_btn.configure(state=DISABLED)
        self._set_status("working")

        self._worker = threading.Thread(target=self._run_extraction,
                                        args=(params,), daemon=True)
        self._worker.start()

    def _run_extraction(self, params: dict) -> None:
        """Worker thread: redirects stdout/stderr to the log queue."""
        redirect = TextRedirector(log_queue)
        try:
            with contextlib.redirect_stdout(redirect), \
                 contextlib.redirect_stderr(redirect):
                params["output_dir"].mkdir(parents=True, exist_ok=True)
                input_dir = params["input_dir"]

                if params["mode"] == "download":
                    asyncio.run(download_bundles(
                        updater_name=params["updater"],
                        working_dir=params["workdir"],
                        patterns=params["patterns"],
                        max_workers=10,
                    ))
                    input_dir = params["workdir"] / "newseer"

                bundles = list(iter_bundle_files(input_dir))
                if not bundles:
                    print(f"[!] No bundle files under {input_dir}")
                    return

                print(f"[*] Found {len(bundles)} bundle(s).")
                seen: set[str] = hash_existing_outputs(params["output_dir"])
                pre_existing = len(seen)
                if pre_existing:
                    print(f"[*] {pre_existing} clip(s) already in output dir — "
                          "duplicates will be skipped.")
                total_e = total_sf = total_sd = 0
                for i, b in enumerate(bundles, 1):
                    rel = b.relative_to(input_dir) if input_dir.is_dir() else b.name
                    print(f"[{i}/{len(bundles)}] {rel}")
                    e, sf, sd = extract_from_bundle(
                        b, params["output_dir"],
                        all_audio=params["all_audio"],
                        verbose=params["verbose"],
                        seen_hashes=seen,
                        tag_with_scene=params["tag_with_scene"],
                    )
                    total_e += e; total_sf += sf; total_sd += sd

                print()
                print(f"[*] Done. New={total_e}  AlreadyOnDisk={pre_existing}  "
                      f"SkippedNonBGM={total_sf}  SkippedDup={total_sd}")
                print(f"[*] Output: {params['output_dir'].resolve()}")
        except Exception:
            log_queue.put("\n[!] Error:\n" + traceback.format_exc())
        finally:
            log_queue.put(None)  # sentinel: worker finished


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _enable_hidpi() -> None:
    """
    Mark the process as DPI-aware on Windows so Tkinter renders crisply on
    HiDPI / "Retina" displays instead of being bitmap-stretched (which
    causes blurry text). Must be called before any Tk window is created.
    No-op on non-Windows platforms — macOS Tk handles Retina natively, and
    Linux relies on the desktop environment's own scaling.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # PROCESS_PER_MONITOR_DPI_AWARE (Windows 8.1+). Handles mixed-DPI
        # setups, e.g. a HiDPI laptop screen + an external 1080p monitor.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        # Older Windows or shcore.dll unavailable: fall back to the
        # system-wide DPI awareness API (Windows Vista+).
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _dpi_scale() -> float:
    """
    Read the current display's DPI scaling factor.
        100% → 1.0     150% → 1.5     200% → 2.0     etc.
    Used to scale fixed pixel measurements (window geometry) so they keep
    the same *physical* size after we declared DPI awareness. Returns 1.0
    on non-Windows or if anything fails.
    """
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        # GetDpiForSystem is Windows 10+
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except AttributeError:
            # Older Windows: GetDeviceCaps(hdc, LOGPIXELSX=88)
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
            ctypes.windll.user32.ReleaseDC(0, hdc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def app_anchor_dir() -> Path:
    """
    The 'home' directory the app should anchor its user-visible files to.

    - When packaged as an exe (PyInstaller `--onefile`): the directory
      *containing the exe*, not the temp _MEIxxxxx dir where bundled data
      gets unpacked. This is what makes the app truly portable — you can
      drop the exe + its data folder anywhere (USB stick, network share,
      `C:\\Apps\\Seer\\bgm`) and the defaults follow.

    - When running from source: the directory containing this script.

    User defaults (working dir, output dir) anchor here. The cwd at launch
    time is NOT used — when a user double-clicks an exe via a shortcut,
    cwd is often `C:\\Windows\\system32` or similar, which would scatter
    downloads in the wrong place.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


def bundled_resource(relative: str) -> Path | None:
    """
    Locate a resource that was bundled into the app at build time (e.g.
    `app_icon.ico`). At runtime, PyInstaller --onefile extracts these into
    `sys._MEIPASS`; when running from source they sit next to this script.

    Returns None if the resource isn't found, so callers can degrade
    gracefully instead of crashing.
    """
    candidates: list[Path] = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / relative)
    candidates.append(Path(__file__).parent / relative)
    candidates.append(app_anchor_dir() / relative)
    for p in candidates:
        if p.exists():
            return p
    return None


def _set_taskbar_identity(app_id: str) -> None:
    """
    On Windows, give the running process its own AppUserModelID so the
    taskbar treats it as a distinct app (with its own icon) instead of
    grouping it under generic 'Python' or whatever happens to be the
    binary name. Must be called before any window is shown. No-op on
    non-Windows.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "window_title": "Seer BGM Extractor",
        "language_label": "Language:",
        # Source frame
        "source_label": "Source",
        "download_radio": "Download from Seer servers",
        "working_dir_label": "Working dir:",
        "updater_name_label": "Updater name:",
        "updater_hint": ("newseer.default = main pack (most BGM)  •  "
                         "newseer.startup = title music  •  "
                         "newseer = all four packs"),
        "patterns_label": "Patterns:",
        "patterns_hint": "(comma-separated globs, blank = all bundles)",
        "use_local_radio": "Use local bundles",
        "path_label": "Path:",
        "browse_button": "Browse…",
        # Output frame
        "output_label": "Output",
        "folder_label": "Folder:",
        # Options frame
        "options_label": "Options",
        "all_audio_check": "Extract all audio (skip BGM filter)",
        "verbose_check": "Verbose logging",
        "scene_tag_check": "Prefix filenames with scene tag (e.g. battle__bgm.wav)",
        # Actions
        "start_button": "Start Extraction",
        "open_output_button": "Open output folder",
        "status_idle": "Idle.",
        "status_working": "Working…",
        "status_done": "Done.",
        # Log frame
        "log_label": "Log",
        # Messageboxes
        "missing_output_title": "Missing output",
        "missing_output_msg": "Choose an output folder.",
        "missing_input_title": "Missing input",
        "missing_input_msg": "Choose a folder of local bundles.",
        "bad_input_title": "Bad input",
        "bad_input_msg": "{path} doesn't exist.",
        "open_folder_title": "Open folder",
        "open_folder_missing": "{path} doesn't exist yet.",
    },
    "zh": {
        "window_title": "赛尔号 BGM 提取器",
        "language_label": "语言:",
        # Source frame
        "source_label": "来源",
        "download_radio": "从赛尔号服务器下载",
        "working_dir_label": "工作目录:",
        "updater_name_label": "更新器名称:",
        "updater_hint": ("newseer.default = 主资源包（大部分 BGM）  •  "
                         "newseer.startup = 标题音乐  •  "
                         "newseer = 全部四个资源包"),
        "patterns_label": "过滤规则:",
        "patterns_hint": "（用逗号分隔的通配符，留空则下载全部资源包）",
        "use_local_radio": "使用本地资源包",
        "path_label": "路径:",
        "browse_button": "浏览…",
        # Output frame
        "output_label": "输出",
        "folder_label": "文件夹:",
        # Options frame
        "options_label": "选项",
        "all_audio_check": "提取所有音频（跳过 BGM 过滤）",
        "verbose_check": "详细日志",
        "scene_tag_check": "在文件名前添加场景标签（如 battle__bgm.wav）",
        # Actions
        "start_button": "开始提取",
        "open_output_button": "打开输出文件夹",
        "status_idle": "空闲",
        "status_working": "处理中…",
        "status_done": "完成",
        # Log frame
        "log_label": "日志",
        # Messageboxes
        "missing_output_title": "缺少输出位置",
        "missing_output_msg": "请选择输出文件夹。",
        "missing_input_title": "缺少输入位置",
        "missing_input_msg": "请选择本地资源包文件夹。",
        "bad_input_title": "输入错误",
        "bad_input_msg": "{path} 不存在。",
        "open_folder_title": "打开文件夹",
        "open_folder_missing": "{path} 尚未创建。",
    },
}

# Mapping for the Combobox display values <-> internal language codes.
# The display values are the language names in their own scripts, which is
# the standard convention for language selectors.
LANG_DISPLAY = {"en": "English", "zh": "中文"}
DISPLAY_TO_LANG = {v: k for k, v in LANG_DISPLAY.items()}


def _detect_default_language() -> str:
    """Guess the user's preferred language from the system locale."""
    try:
        lang, _ = locale.getlocale()
        if lang and lang.lower().startswith("zh"):
            return "zh"
    except Exception:
        pass
    return "en"


def _config_path() -> Path:
    """Where the small settings file lives. Anchored to the exe for portability."""
    return app_anchor_dir() / "seer_bgm_config.json"


def _load_config() -> dict:
    """Load user settings. Empty dict if missing/corrupt."""
    p = _config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(cfg: dict) -> None:
    """Save user settings. Silent if the disk write fails."""
    try:
        _config_path().write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def main() -> int:
    _enable_hidpi()
    # Distinct AppUserModelID → taskbar treats us as our own app and uses
    # our window icon instead of grouping under 'python' or 'pythonw'.
    _set_taskbar_identity("Anthropic.SeerBGMExtractor")
    root = Tk()
    # Use a nicer ttk theme where available
    try:
        style = ttk.Style()
        themes = style.theme_names()
        if sys.platform.startswith("win") and "vista" in themes:
            style.theme_use("vista")
        elif "clam" in themes:
            style.theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
