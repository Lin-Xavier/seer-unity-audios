#!/usr/bin/env python3
"""
extract_seer_bgm.py
===================
Extract background music (BGM) from the Seer Unity client.

Pipeline:
    1. (optional) Use albi0 to download Seer's Unity asset bundles.
    2. Walk the bundles with UnityPy, locate every AudioClip object.
    3. Filter using BGM heuristics (filename patterns + clip length).
    4. Save extracted audio to an output directory as WAV (or OGG / FSB
       depending on how Unity packed it).

Requirements:
    pip install albi0 UnityPy fsb5

Usage examples:
    # End-to-end: download bundles, then extract BGM
    python extract_seer_bgm.py --download -o ./bgm

    # Extract from an existing bundle directory
    python extract_seer_bgm.py ./seer_workspace/seerproject -o ./bgm

    # Extract every AudioClip (no BGM filtering)
    python extract_seer_bgm.py ./bundles --all-audio -o ./all_audio

    # Restrict the download to bundles whose names match a pattern
    python extract_seer_bgm.py --download --pattern "*audio*" --pattern "*sound*"
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from pathlib import Path
from typing import Iterable

try:
    import UnityPy
except ImportError:
    UnityPy = None  # checked lazily by callers


def app_anchor_dir() -> Path:
    """
    Directory used as the anchor for default user-visible paths (workdir,
    output dir). When packaged as a PyInstaller exe, this is the directory
    containing the exe — so the tool is portable: drop the exe anywhere
    (USB stick, network share, any folder) and the defaults follow.
    When running from source, this is the directory of this script.
    Notably this is *not* cwd, which is unpredictable when launching from
    a shortcut (often C:\\Windows\\system32 or similar).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


# ---------------------------------------------------------------------------
# BGM detection heuristics
# ---------------------------------------------------------------------------

# Positive signals: names that strongly imply music
BGM_NAME_PATTERNS = [
    re.compile(r"bgm", re.IGNORECASE),
    re.compile(r"music", re.IGNORECASE),
    re.compile(r"\bbg[_\-]", re.IGNORECASE),
    re.compile(r"theme", re.IGNORECASE),
    re.compile(r"battle.*music", re.IGNORECASE),
    re.compile(r"ost", re.IGNORECASE),
]

# Negative signals: names that look like sound effects / voice
SFX_NAME_PATTERNS = [
    re.compile(r"^sfx[_\-]", re.IGNORECASE),
    re.compile(r"^se[_\-]", re.IGNORECASE),
    re.compile(r"voice", re.IGNORECASE),
    re.compile(r"click", re.IGNORECASE),
    re.compile(r"hit", re.IGNORECASE),
    re.compile(r"hover", re.IGNORECASE),
    re.compile(r"button", re.IGNORECASE),
]

# Fallback: clips at least this long are usually music.
# Seer SFX are typically < 5 seconds; BGM tracks are tens of seconds or more.
MIN_BGM_DURATION_SEC = 15.0


def is_likely_bgm(name: str, length_sec: float | None) -> bool:
    """Heuristic: decide whether an AudioClip looks like BGM."""
    if any(p.search(name) for p in SFX_NAME_PATTERNS):
        return False
    if any(p.search(name) for p in BGM_NAME_PATTERNS):
        return True
    if length_sec is not None and length_sec >= MIN_BGM_DURATION_SEC:
        return True
    return False


# ---------------------------------------------------------------------------
# Cross-run dedup (so weekly re-runs don't recreate duplicates)
# ---------------------------------------------------------------------------

AUDIO_OUTPUT_EXTENSIONS = (".wav", ".ogg", ".mp3", ".fsb")


def hash_existing_outputs(output_dir: Path) -> set[str]:
    """
    SHA1-hash every audio file already in output_dir.

    Returns a set of digests suitable for seeding the in-run dedup. When
    re-running after a game update, anything whose decoded bytes match a
    file already on disk will be skipped — only genuinely new BGM gets
    written.
    """
    seen: set[str] = set()
    if not output_dir.exists():
        return seen
    for f in output_dir.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in AUDIO_OUTPUT_EXTENSIONS:
            continue
        try:
            seen.add(hashlib.sha1(f.read_bytes()).hexdigest())
        except OSError:
            continue
    return seen


# ---------------------------------------------------------------------------
# Bundle scanning
# ---------------------------------------------------------------------------

BUNDLE_EXTENSIONS = (".ab", ".bundle", ".unity3d", ".assets", ".bytes")
UNITY_MAGIC = (b"UnityFS", b"UnityRaw", b"UnityWeb", b"UnityArc")


def _looks_like_unity_bundle(path: Path) -> bool:
    """Cheap check: does this file start with a Unity AssetBundle magic header?"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    return any(head.startswith(m) for m in UNITY_MAGIC)


def iter_bundle_files(input_path: Path) -> Iterable[Path]:
    """
    Yield every Unity bundle under input_path.

    Detection is permissive: we accept any file with a known extension OR
    any file whose first bytes match a Unity AssetBundle magic header.
    The newseer plugin uses YooAsset, which sometimes writes hash-named
    files with no extension — magic-byte detection catches those.
    """
    if input_path.is_file():
        yield input_path
        return

    for f in input_path.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() in BUNDLE_EXTENSIONS:
            yield f
            continue
        # Skip obvious non-bundles cheaply
        if f.suffix.lower() in (".json", ".hash", ".version", ".manifest", ".txt"):
            continue
        if f.stat().st_size < 32:
            continue
        if _looks_like_unity_bundle(f):
            yield f


def safe_filename(name: str) -> str:
    """Strip characters that are illegal on most filesystems."""
    return re.sub(r'[<>:"/\\|?*\0]', "_", name).strip() or "unnamed"


# Boilerplate tokens to strip from bundle names when deriving a scene tag.
# Order matters; longer prefixes first so we don't half-strip them.
_SCENE_TAG_STRIP_PREFIXES = ("art_ui_", "art_", "ui_")
_SCENE_TAG_STRIP_TOKENS = (
    "_audio", "audio_",
    "_image_res", "image_res_",
    "_bgm", "bgm_",
    "_music", "music_",
    "_sfx", "sfx_",
    "_cv", "cv_",
)


def scene_tag_from_bundle(bundle_path: Path, root: Path | None = None) -> str:
    """
    Derive a 'scene-ish' tag from a bundle's path, used as a filename prefix.

    Seer's bundles follow naming like:
        art_autocard_audio_bgm_autocard_bgm        → 'autocard'
        art_ui_mainstoryline_image_res_music_01_bgm → 'mainstoryline'
        assetbundles/DefaultPackage/art_battle_bgm  → 'battle'

    This is the fallback path: used when a clip has no m_Container entry.
    For Seer the better source is the Unity project path — see
    scene_tag_from_container_path() below.
    """
    # Use the bundle's filename (without extension) as the basis
    stem = bundle_path.stem.lower()

    # Strip obvious prefixes
    for prefix in _SCENE_TAG_STRIP_PREFIXES:
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break

    # Strip boilerplate tokens anywhere in the name
    for token in _SCENE_TAG_STRIP_TOKENS:
        stem = stem.replace(token, "_")

    # Collapse runs of underscores and trim
    stem = re.sub(r"_+", "_", stem).strip("_")

    # Take just the first meaningful component as the scene tag
    parts = stem.split("_")
    tag = parts[0] if parts and parts[0] else "misc"

    return safe_filename(tag)


# Generic path components ignored when deriving a scene tag from a
# Unity project path. The 'scene' is conceptually the first directory
# component that *isn't* one of these.
_GENERIC_PATH_TOKENS = frozenset({
    "assets", "art", "audio", "bgm", "music", "sound", "sfx",
    "cv", "voice", "image", "res", "ui", "common", "shared",
})


def scene_tag_from_container_path(path: str) -> str | None:
    """
    Derive a scene tag from a Unity project path. This is the *preferred*
    source — m_Container paths are the canonical paths Unity uses for the
    assets and are more reliable than parsing bundle filenames.

    Examples (from real Seer bundles):
        'assets/art/autocard/audio/bgm/autocard_bgm.mp3'   → 'autocard'
        'assets/art/ui/assets/mainstoryline/image/res/music/01_bgm.mp3'
                                                           → 'mainstoryline'
        'assets/audio/bgm/something.mp3'                   → None (generic)

    Returns None when no non-generic component exists, so the caller can
    fall back to scene_tag_from_bundle().
    """
    if not path:
        return None
    s = path.lower().replace("\\", "/")
    # Drop the filename — we want directory context
    if "/" in s:
        s = s.rsplit("/", 1)[0]
    # First non-generic directory component wins
    for component in s.split("/"):
        if component and component not in _GENERIC_PATH_TOKENS:
            return safe_filename(component)
    return None


def extract_from_bundle(
    bundle_path: Path,
    output_dir: Path,
    *,
    all_audio: bool,
    verbose: bool,
    seen_hashes: set[str],
    tag_with_scene: bool = True,
) -> tuple[int, int, int]:
    """
    Extract AudioClips from one bundle.
    Returns (extracted, skipped_non_bgm, skipped_duplicate).

    When `tag_with_scene` is True (default), output filenames are prefixed
    with a scene tag derived from the bundle path, e.g.
        autocard__BGM_Hall.wav
    Files from the same scene then sort together in the output folder.
    """
    extracted = skipped_non_bgm = skipped_dup = 0

    if UnityPy is None:
        raise RuntimeError(
            "UnityPy is not installed. Run: pip install UnityPy fsb5"
        )

    try:
        env = UnityPy.load(str(bundle_path))
    except Exception as exc:
        print(f"  [!] Could not load {bundle_path.name}: {exc}", file=sys.stderr)
        return 0, 0, 0

    # First pass: build a map of path_id → Unity project path from this
    # bundle's m_Container. The container path is the canonical Unity
    # project path for the asset (e.g. assets/art/autocard/audio/bgm/...),
    # which beats parsing the bundle filename as a scene-tag source.
    container_paths: dict[int, str] = {}
    if tag_with_scene:
        for obj in env.objects:
            if obj.type.name != "AssetBundle":
                continue
            try:
                ab = obj.read()
            except Exception:
                break
            for entry in (getattr(ab, "m_Container", None) or []):
                if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                    continue
                path_str, asset_info = entry
                pptr = getattr(asset_info, "asset", None)
                pid = getattr(pptr, "m_PathID", None) if pptr else None
                if pid is not None and isinstance(path_str, str):
                    container_paths[pid] = path_str
            break  # at most one AssetBundle object per bundle

    # Fallback when a clip has no container entry: derive from the bundle name.
    fallback_tag = scene_tag_from_bundle(bundle_path) if tag_with_scene else None

    for obj in env.objects:
        if obj.type.name != "AudioClip":
            continue

        try:
            clip = obj.read()
        except Exception as exc:
            print(f"  [!] Could not read AudioClip in {bundle_path.name}: {exc}",
                  file=sys.stderr)
            continue

        name = getattr(clip, "m_Name", "") or f"audio_{obj.path_id}"
        length = getattr(clip, "m_Length", None)

        # Apply BGM filter unless user opted out
        if not all_audio and not is_likely_bgm(name, length):
            skipped_non_bgm += 1
            if verbose:
                dur = f"{length:.1f}s" if length else "?"
                print(f"  [-] skip non-BGM: {name} ({dur})")
            continue

        # UnityPy returns dict[str, bytes]; bytes are WAV when fsb5 is available.
        try:
            samples = clip.samples
        except Exception as exc:
            print(f"  [!] No samples for {name}: {exc}", file=sys.stderr)
            continue

        if not samples:
            print(f"  [!] Empty samples for {name} "
                  "(install `fsb5` if you see this often)", file=sys.stderr)
            continue

        # Determine scene tag for *this* clip: prefer the Unity project
        # path from m_Container; fall back to the bundle-name heuristic.
        if tag_with_scene:
            container_path = container_paths.get(obj.path_id)
            clip_scene_tag = (
                scene_tag_from_container_path(container_path)
                if container_path else None
            )
            if clip_scene_tag is None:
                clip_scene_tag = fallback_tag
            if verbose and container_path:
                print(f"      [path] {container_path}")
        else:
            clip_scene_tag = None

        for sample_name, sample_data in samples.items():
            # Deduplicate across bundles by content hash
            digest = hashlib.sha1(sample_data).hexdigest()
            if digest in seen_hashes:
                skipped_dup += 1
                if verbose:
                    print(f"  [-] skip duplicate: {sample_name}")
                continue
            seen_hashes.add(digest)

            # Ensure a sensible filename + extension
            base = safe_filename(sample_name)
            if "." not in base:
                base += ".wav"

            # Prepend scene tag unless the clip name already contains it
            if clip_scene_tag and clip_scene_tag not in base.lower():
                stem, suffix = Path(base).stem, Path(base).suffix
                out_name = f"{clip_scene_tag}__{stem}{suffix}"
            else:
                out_name = base

            out_path = output_dir / out_name
            counter = 1
            while out_path.exists():
                stem, suffix = Path(out_name).stem, Path(out_name).suffix
                out_path = output_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            out_path.write_bytes(sample_data)
            extracted += 1
            print(f"  [+] {out_path.name}  ({len(sample_data) / 1024:.1f} KB)")

    return extracted, skipped_non_bgm, skipped_dup


# ---------------------------------------------------------------------------
# Albi0 integration (optional download step)
# ---------------------------------------------------------------------------

async def download_bundles(
    updater_name: str,
    working_dir: Path,
    patterns: list[str],
    max_workers: int,
) -> None:
    """Use albi0 to fetch the Seer Unity asset bundles."""
    try:
        import albi0
    except ImportError:
        sys.exit("albi0 is not installed. Run: pip install albi0")

    print(f"[*] Downloading Seer Unity bundles ({updater_name}) -> {working_dir}")
    working_dir.mkdir(parents=True, exist_ok=True)

    async with albi0.session():
        # The `newseer` plugin handles the Seer Unity client (newseer.61.com).
        # Note: `seerproject` is a DIFFERENT game (赛尔计划 / Seer Plan) — don't use it.
        albi0.load_plugin("newseer")
        await albi0.update_resources(
            updater_name,
            *patterns,
            working_dir=str(working_dir),
            max_workers=max_workers,
        )
    print("[*] Download complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract BGM (background music) from Seer Unity asset bundles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="Path to a .ab file or a directory containing bundles. "
             "Optional if --download is used.",
    )
    p.add_argument(
        "-o", "--output",
        type=Path,
        default=app_anchor_dir() / "bgm_out",
        help="Output directory (default: ./bgm_out next to the script/exe)",
    )
    p.add_argument(
        "--download",
        action="store_true",
        help="Download bundles via albi0 before extracting.",
    )
    p.add_argument(
        "--updater-name",
        default="newseer.default",
        help="albi0 updater name for the Seer Unity client. Available:\n"
             "  newseer.default  — DefaultPackage (most BGM lives here)\n"
             "  newseer.startup  — StartupPackage (title / opening music)\n"
             "  newseer.config   — ConfigPackage (config data; no audio)\n"
             "  newseer.pet      — PetAnimPackage (pet animations; mostly SFX)\n"
             "  newseer          — all four packages\n"
             "(default: newseer.default)",
    )
    p.add_argument(
        "--workdir",
        type=Path,
        default=app_anchor_dir() / "seer_workspace",
        help="Working directory for downloads "
             "(default: ./seer_workspace next to the script/exe)",
    )
    p.add_argument(
        "--pattern",
        action="append",
        default=[],
        metavar="GLOB",
        help="Restrict download to filenames matching GLOB. Repeatable. "
             "Example: --pattern '*audio*' --pattern '*sound*'",
    )
    p.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Concurrent download workers (default: 10).",
    )
    p.add_argument(
        "--all-audio",
        action="store_true",
        help="Extract every AudioClip, skipping the BGM heuristic filter.",
    )
    p.add_argument(
        "--no-scene-tag",
        action="store_true",
        help="Don't prefix output filenames with a scene tag derived from "
             "the bundle path. Default behaviour names files like "
             "'<scene>__<clip>.wav' so same-scene tracks sort together.",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print skipped clips.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 1. Optional download
    if args.download:
        asyncio.run(download_bundles(
            updater_name=args.updater_name,
            working_dir=args.workdir,
            patterns=args.pattern,
            max_workers=args.max_workers,
        ))
        if args.input is None:
            # albi0's newseer plugin writes to <workdir>/newseer/assetbundles/...
            args.input = args.workdir / "newseer"

    # 2. Validate input
    if args.input is None or not args.input.exists():
        print("[!] No input path. Pass a bundle/directory or use --download.",
              file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)

    bundles = list(iter_bundle_files(args.input))
    if not bundles:
        print(f"[!] No bundle files found under {args.input}", file=sys.stderr)
        return 1

    # 3. Extract
    print(f"[*] Found {len(bundles)} bundle(s). Scanning for AudioClips...")
    seen_hashes: set[str] = hash_existing_outputs(args.output)
    pre_existing = len(seen_hashes)
    if pre_existing:
        print(f"[*] {pre_existing} clip(s) already in output dir — duplicates will be skipped.")
    total_extracted = total_skip_filter = total_skip_dup = 0

    for i, bundle in enumerate(bundles, 1):
        rel = bundle.relative_to(args.input) if args.input.is_dir() else bundle.name
        print(f"[{i}/{len(bundles)}] {rel}")
        e, sf, sd = extract_from_bundle(
            bundle, args.output,
            all_audio=args.all_audio,
            verbose=args.verbose,
            seen_hashes=seen_hashes,
            tag_with_scene=not args.no_scene_tag,
        )
        total_extracted += e
        total_skip_filter += sf
        total_skip_dup += sd

    # 4. Summary
    print()
    print(f"[*] Done.")
    print(f"    New this run     : {total_extracted}")
    print(f"    Already on disk  : {pre_existing}")
    print(f"    Skipped (non-BGM): {total_skip_filter}")
    print(f"    Skipped (in-run) : {total_skip_dup}")
    print(f"    Output           : {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
