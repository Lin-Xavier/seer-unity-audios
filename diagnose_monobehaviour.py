#!/usr/bin/env python3
"""
diagnose_monobehaviour.py
=========================
Investigate whether Seer's MonoBehaviour scene-config assets are extractable.

This is *step 1 of 2*. It only inspects — nothing is written.

For each bundle (up to --limit), report:
  1. Whether the bundle exposes TypeTree info for MonoBehaviours.
     This is the make-or-break factor — without TypeTrees, MonoBehaviour
     fields can't be deserialized by name, only as raw bytes.
  2. The script class names of MonoBehaviour objects.
     (Resolved via the m_Script PPtr → MonoScript.m_ClassName.)
  3. Whether each MonoBehaviour's raw byte payload contains any path-id
     that points to an AudioClip — a signal that the script holds a
     scene→clip mapping.
  4. Aggregated summary: which script types reference audio, and how
     often. The top scripts by audio-ref count are the candidates worth
     reverse-engineering.

Use: scan the same DefaultPackage you already downloaded.

    python diagnose_monobehaviour.py "C:\\Apps\\Seer\\bgm\\dist\\seer_workspace\\newseer\\assetbundles\\DefaultPackage" --limit 100
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import UnityPy
except ImportError:
    sys.exit("UnityPy not installed. Run: pip install UnityPy")

sys.path.insert(0, str(Path(__file__).parent))
try:
    from extract_seer_bgm import iter_bundle_files
except ImportError:
    sys.exit("Could not import extract_seer_bgm.py — keep this script next to it.")


def _safe_get(obj, attr, default=None):
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def _has_typetree(obj) -> bool:
    """
    Crude check for whether this object's serialized type carries a TypeTree.
    Different UnityPy versions expose this differently, so we try a few.
    """
    st = _safe_get(obj, "serialized_type", None)
    if st is None:
        return False
    nodes = _safe_get(st, "m_Nodes", None) or _safe_get(st, "nodes", None)
    return bool(nodes)


def _resolve_script_name(mono_obj) -> str:
    """Follow m_Script PPtr to the MonoScript and return m_ClassName, if possible."""
    try:
        mb = mono_obj.read()
    except Exception:
        return "<unreadable>"
    script_pptr = _safe_get(mb, "m_Script", None)
    if not script_pptr:
        return "<no script ptr>"
    # PPtr-resolve. UnityPy exposes a couple of patterns.
    target = None
    for attr in ("read", "deref"):
        fn = getattr(script_pptr, attr, None)
        if callable(fn):
            try:
                target = fn()
                break
            except Exception:
                target = None
    if target is None:
        return "<unresolved>"
    return _safe_get(target, "m_ClassName", "<no classname>") or "<empty>"


def _find_pathid_audioclip_hits(raw_bytes: bytes,
                                audioclip_path_ids: set[int]) -> int:
    """
    Brute-force: scan the raw MonoBehaviour bytes for any 8-byte little-endian
    integer matching a known AudioClip path_id. PPtrs in Unity binary data
    are stored as (file_id: i32, path_id: i64) tuples; we only check the
    path_id half, which is good enough as a signal.

    Returns the count of matching hits.
    """
    if not audioclip_path_ids or not raw_bytes:
        return 0
    hits = 0
    # Walk 8-byte windows on 4-byte alignment (Unity aligns)
    limit = len(raw_bytes) - 8
    for offset in range(0, limit, 4):
        pid = struct.unpack_from("<q", raw_bytes, offset)[0]
        if pid in audioclip_path_ids:
            hits += 1
    return hits


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path)
    p.add_argument("--limit", type=int, default=100,
                   help="Stop after N bundles inspected (default 100). The "
                        "more we scan, the more accurate the script-type stats.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if not args.input.exists():
        print(f"[!] {args.input} doesn't exist", file=sys.stderr)
        return 1

    bundles = list(iter_bundle_files(args.input))
    print(f"[*] Found {len(bundles)} bundle file(s). Inspecting up to {args.limit}.")
    print()

    # Stats
    bundles_with_typetree = 0
    bundles_with_mono = 0
    bundles_with_audioref_mono = 0
    mono_total = 0
    mono_with_audio_refs = 0
    script_audio_hits: Counter[str] = Counter()  # script class → bundles where it refs audio
    script_total: Counter[str] = Counter()       # script class → total instances seen
    script_audio_instances: defaultdict[str, list[tuple[str, int]]] = defaultdict(list)
    # ^ for top-scoring scripts, remember (bundle_name, hit_count) examples

    for i, bundle in enumerate(bundles):
        if i >= args.limit:
            break
        try:
            env = UnityPy.load(str(bundle))
        except Exception as exc:
            if args.verbose:
                print(f"  [!] {bundle.name}: load failed: {exc}", file=sys.stderr)
            continue

        # Collect AudioClip path_ids in this bundle
        audio_pids: set[int] = set()
        mono_objs = []
        bundle_has_typetree = False
        for obj in env.objects:
            t = obj.type.name
            if t == "AudioClip":
                audio_pids.add(obj.path_id)
            elif t == "MonoBehaviour":
                mono_objs.append(obj)
                if _has_typetree(obj):
                    bundle_has_typetree = True

        if bundle_has_typetree:
            bundles_with_typetree += 1
        if mono_objs:
            bundles_with_mono += 1
        if not mono_objs:
            continue

        bundle_audio_refs_found = False
        for mono in mono_objs:
            mono_total += 1
            script_name = _resolve_script_name(mono)
            script_total[script_name] += 1

            # Get raw bytes for hit-scan
            try:
                raw = mono.get_raw_data() if hasattr(mono, "get_raw_data") else None
            except Exception:
                raw = None
            if raw is None:
                # Older UnityPy stores raw under different attrs
                raw = _safe_get(mono, "raw_data", None) or b""

            hits = _find_pathid_audioclip_hits(raw, audio_pids) if audio_pids else 0
            if hits > 0:
                mono_with_audio_refs += 1
                script_audio_hits[script_name] += hits
                bundle_audio_refs_found = True
                if len(script_audio_instances[script_name]) < 5:
                    script_audio_instances[script_name].append((bundle.name, hits))
                if args.verbose:
                    print(f"  [+] {bundle.name}: {script_name!r} "
                          f"refs {hits} AudioClip(s)")

        if bundle_audio_refs_found:
            bundles_with_audioref_mono += 1

    # Report
    inspected = min(len(bundles), args.limit)
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Bundles inspected                        : {inspected}")
    print(f"  ... with at least one MonoBehaviour       : {bundles_with_mono}")
    print(f"  ... with TypeTree info for MonoBehaviours : {bundles_with_typetree}")
    print(f"  ... where a MonoBehaviour references audio: {bundles_with_audioref_mono}")
    print()
    print(f"  MonoBehaviour instances total            : {mono_total}")
    print(f"  ... that reference >=1 AudioClip          : {mono_with_audio_refs}")
    print()

    print("Top script classes by total instance count:")
    for name, n in script_total.most_common(15):
        ref_n = script_audio_hits.get(name, 0)
        marker = " ← references audio" if ref_n > 0 else ""
        print(f"  {n:>5}  {name}{marker}")
    print()

    if script_audio_hits:
        print("Script classes that reference AudioClips (candidates for extraction):")
        for name, ref_count in script_audio_hits.most_common(15):
            instances = script_total.get(name, 0)
            print(f"  {ref_count:>5} refs from {instances:>4} instances  -  {name}")
            for bname, hits in script_audio_instances[name][:3]:
                print(f"             example: {bname} ({hits} ref{'s' if hits != 1 else ''})")
    else:
        print("No MonoBehaviour was found referencing an AudioClip in this sample.")
        print("(This often just means the configs live in a different package —")
        print(" try `newseer.config` or `newseer.startup` instead of DefaultPackage.)")
    print()

    # Key verdict
    print("VERDICT")
    print("=" * 70)
    typetree_pct = (100 * bundles_with_typetree / inspected) if inspected else 0
    audio_ref_pct = (100 * bundles_with_audioref_mono / inspected) if inspected else 0
    print(f"  TypeTree availability  : {typetree_pct:.0f}% of bundles")
    print(f"  Audio-referencing MBs  : {audio_ref_pct:.0f}% of bundles")
    print()
    if typetree_pct >= 80 and script_audio_hits:
        print("  → Extractable. TypeTrees present + audio refs found.")
        print("    Worth proceeding to step 2 (write the extractor).")
    elif not script_audio_hits:
        print("  → No audio refs in this package. Scan newseer.config / startup.")
    elif typetree_pct < 30:
        print("  → IL2CPP/stripped build: TypeTrees mostly absent.")
        print("    Would need raw struct reverse-engineering of the top script")
        print("    types. Doable but a much bigger project than option 1 was.")
    else:
        print("  → Partial coverage. Some configs decodable, some not.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
