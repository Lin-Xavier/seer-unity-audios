#!/usr/bin/env python3
"""
diagnose_scene_info.py
======================
Read-only inspection of Seer asset bundles to see what scene-context info
is actually available. Prints, for each bundle containing a BGM-named
AudioClip:

  1. The bundle's own m_Container paths (Unity project paths for assets)
  2. Any AudioSource components that reference the clip, and the names of
     the GameObjects they live on

Output is purely informational. Nothing is extracted, modified, or saved.

Usage:
    python diagnose_scene_info.py <bundles_dir> [--limit N]

Example (using the folder downloaded by the main tool):
    python diagnose_scene_info.py "C:\\Apps\\Seer\\bgm\\dist\\seer_workspace\\newseer\\assetbundles\\DefaultPackage" --limit 20
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import UnityPy
except ImportError:
    sys.exit("UnityPy not installed. Run: pip install UnityPy")

# Reuse the BGM heuristic and bundle-scanning from the main tool so this
# diagnostic looks at the same files extraction would target.
sys.path.insert(0, str(Path(__file__).parent))
try:
    from extract_seer_bgm import is_likely_bgm, iter_bundle_files
except ImportError:
    sys.exit("Could not import extract_seer_bgm.py — keep this script next to it.")


def safe_get(obj, attr, default=None):
    """obj.attr but never raises."""
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def inspect_bundle(bundle_path: Path, all_audio: bool, verbose: bool) -> dict | None:
    """
    Return a small dict summarising what scene-context info we found in this
    bundle, or None if the bundle has no BGM-named clips to investigate.
    """
    try:
        env = UnityPy.load(str(bundle_path))
    except Exception as exc:
        if verbose:
            print(f"  [!] load failed: {exc}", file=sys.stderr)
        return None

    # First pass: find AudioClips of interest and remember their path_ids
    interesting_clip_pids: dict[int, str] = {}  # path_id → clip name
    all_audio_pids: dict[int, str] = {}         # path_id → clip name (for joining)
    for obj in env.objects:
        if obj.type.name != "AudioClip":
            continue
        try:
            clip = obj.read()
        except Exception:
            continue
        name = safe_get(clip, "m_Name", "") or f"audio_{obj.path_id}"
        length = safe_get(clip, "m_Length", None)
        all_audio_pids[obj.path_id] = name
        if all_audio or is_likely_bgm(name, length):
            interesting_clip_pids[obj.path_id] = name

    if not interesting_clip_pids:
        return None

    summary = {
        "bundle": bundle_path.name,
        "clips": interesting_clip_pids,
        "container_paths": {},   # path_id → Unity project path
        "audiosources": [],      # list of {go_name, clip_pid, clip_name}
    }

    # m_Container lookup — every bundle has at most one AssetBundle object
    for obj in env.objects:
        if obj.type.name != "AssetBundle":
            continue
        try:
            ab = obj.read()
        except Exception:
            continue
        container = safe_get(ab, "m_Container", None) or []
        # m_Container is List[Tuple[str, AssetInfo]]; AssetInfo has 'asset' PPtr
        for entry in container:
            try:
                # Tuple unpacking — UnityPy sometimes returns list-of-pairs
                if isinstance(entry, (tuple, list)) and len(entry) == 2:
                    path_str, asset_info = entry
                else:
                    continue
            except Exception:
                continue
            pptr = safe_get(asset_info, "asset", None)
            pid = safe_get(pptr, "m_PathID", None) if pptr is not None else None
            if pid in interesting_clip_pids:
                summary["container_paths"][pid] = path_str
        break  # only one AssetBundle object per bundle

    # AudioSource → AudioClip references (within this bundle only — cross-bundle
    # PPtr resolution is what would need a shared environment, not done here)
    for obj in env.objects:
        if obj.type.name != "AudioSource":
            continue
        try:
            src = obj.read()
        except Exception:
            continue
        clip_pptr = safe_get(src, "m_audioClip", None)
        clip_pid = safe_get(clip_pptr, "m_PathID", None) if clip_pptr else None
        if clip_pid not in interesting_clip_pids:
            continue

        # GameObject name
        go_name = "<unknown>"
        go_pptr = safe_get(src, "m_GameObject", None)
        go_pid = safe_get(go_pptr, "m_PathID", None) if go_pptr else None
        if go_pid:
            for obj2 in env.objects:
                if obj2.path_id == go_pid and obj2.type.name == "GameObject":
                    try:
                        go = obj2.read()
                        go_name = safe_get(go, "m_Name", "<unnamed>") or "<unnamed>"
                    except Exception:
                        pass
                    break

        summary["audiosources"].append({
            "go_name": go_name,
            "clip_pid": clip_pid,
            "clip_name": interesting_clip_pids[clip_pid],
        })

    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path, help="Path to bundles directory")
    p.add_argument("--limit", type=int, default=20,
                   help="Stop after N bundles with BGM (default 20)")
    p.add_argument("--all-audio", action="store_true",
                   help="Inspect every AudioClip, not just BGM-named ones")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if not args.input.exists():
        print(f"[!] {args.input} doesn't exist", file=sys.stderr)
        return 1

    bundles = list(iter_bundle_files(args.input))
    print(f"[*] Found {len(bundles)} bundle file(s) total. "
          f"Stopping after {args.limit} with BGM.")
    print()

    # Aggregate stats
    total_clips = 0
    clips_with_container = 0
    clips_with_audiosource = 0
    container_path_samples: list[str] = []
    audiosource_samples: list[tuple[str, str]] = []  # (go_name, clip_name)
    inspected = 0

    for bundle in bundles:
        info = inspect_bundle(bundle, args.all_audio, args.verbose)
        if not info:
            continue
        inspected += 1
        if inspected > args.limit:
            break

        print(f"=== [{inspected}] {info['bundle']} ===")
        print(f"  BGM-ish clips: {len(info['clips'])}")
        for pid, name in info["clips"].items():
            total_clips += 1
            container_path = info["container_paths"].get(pid)
            if container_path:
                clips_with_container += 1
                container_path_samples.append(container_path)
                print(f"    • {name!r}")
                print(f"        m_Container path: {container_path}")
            else:
                print(f"    • {name!r}  (no container path)")

        if info["audiosources"]:
            print(f"  AudioSource components referencing these clips:")
            for src in info["audiosources"]:
                clips_with_audiosource += 1
                audiosource_samples.append((src["go_name"], src["clip_name"]))
                print(f"    • GameObject {src['go_name']!r}  →  "
                      f"clip {src['clip_name']!r}")
        else:
            print(f"  (no AudioSource components reference these clips "
                  f"within this bundle)")
        print()

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Bundles inspected with BGM clips : {inspected}")
    print(f"  Total BGM-ish clips seen          : {total_clips}")
    print(f"  Clips with m_Container path       : {clips_with_container}"
          f"  ({_pct(clips_with_container, total_clips)})")
    print(f"  Clips with AudioSource reference  : {clips_with_audiosource}"
          f"  ({_pct(clips_with_audiosource, total_clips)})")
    print()

    if container_path_samples:
        print("Sample m_Container paths (up to 10):")
        for s in container_path_samples[:10]:
            print(f"  {s}")
        # Heuristic: would these paths give us better scene tags than bundle names?
        print()
        print("Candidate scene tags derived from these paths:")
        for s in container_path_samples[:10]:
            print(f"  {s!r}  →  {_derive_tag_from_path(s)!r}")
        print()

    if audiosource_samples:
        print("Sample AudioSource GameObject names (up to 10):")
        for go, clip in audiosource_samples[:10]:
            print(f"  GameObject {go!r}  plays  {clip!r}")
        print()

    return 0


def _pct(n: int, total: int) -> str:
    return f"{(100*n/total):.0f}%" if total else "n/a"


def _derive_tag_from_path(path: str) -> str:
    """
    Quick-and-dirty: turn a Unity project path into a scene-ish tag.
    This is preview-only — the real implementation will refine these rules
    once we see what the actual paths look like.
    """
    s = path.lower().replace("\\", "/")
    # Strip the leading "assets/" prefix
    if s.startswith("assets/"):
        s = s[len("assets/"):]
    # Drop the filename — we want the directory structure
    if "/" in s:
        s = s.rsplit("/", 1)[0]
    # Strip noisy directories
    for noise in ("audio/", "/audio", "bgm/", "/bgm", "music/", "/music",
                  "sound/", "/sound", "art/", "/art"):
        s = s.replace(noise, "/")
    s = re.sub(r"/+", "/", s).strip("/")
    # Use the first meaningful component as the tag
    parts = [p for p in s.split("/") if p and p not in ("res", "image", "ui")]
    return parts[0] if parts else "misc"


if __name__ == "__main__":
    sys.exit(main())
