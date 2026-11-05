#!/usr/bin/env python3
"""A/B regression check: run detect_ad_blocks on one episode with two
versions of pipeline/run_pipeline.py and diff the output.

Guards detector changes against regressions on the live pipeline's own
shows (e.g. Security Now): the new code must not mark wildly different
blocks on episodes the old code handled well.

Usage:
  python3 eval/regression_ab.py \
      --new pipeline/run_pipeline.py \
      --old /path/to/run_pipeline.py   # e.g. `git show main:pipeline/run_pipeline.py`
      --words eval_work/blab/<vid>/transcript/transcript.json \
      --audio eval_work/blab/<vid>/<vid>.webm \
      [--description file.html]         # optional, for extract_sponsors A/B

Words JSON format: {"words": [[start, end, text], ...], "duration": s}
(the format run_blab_eval.py / transcribe_chunked produce).
"""

import argparse
import importlib.util
import json
import os
import sys


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    os.environ.pop("HF_HUB_OFFLINE", None)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", default="pipeline/run_pipeline.py")
    ap.add_argument("--old", required=True,
                    help="path to the old run_pipeline.py "
                         "(git show main:pipeline/run_pipeline.py > old.py)")
    ap.add_argument("--words", required=True)
    ap.add_argument("--audio")
    ap.add_argument("--description")
    ap.add_argument("--work-dir", default="regression_work")
    args = ap.parse_args()

    with open(args.words) as f:
        data = json.load(f)
    words = [list(w) for w in data["words"]]
    description = ""
    if args.description and os.path.exists(args.description):
        with open(args.description) as f:
            description = f.read()

    results = {}
    for label, path in (("old", args.old), ("new", args.new)):
        mod = load_module(path, f"rp_{label}")
        os.makedirs(args.work_dir, exist_ok=True)
        sponsors = mod.extract_sponsors(description) if description else []
        silences = mod.find_silences(args.audio, args.work_dir,
                                     min_dur=0.8) if args.audio else []
        blocks, suspects = mod.detect_ad_blocks(
            words, sponsors, silences, args.audio, [])
        results[label] = {
            "sponsors": [s["label"] for s in sponsors],
            "blocks": [[round(s, 1), round(e, 1), l] for s, e, l in blocks],
            "suspects": [[round(s, 1), round(e, 1), l] for s, e, l in suspects],
        }

    old, new = results["old"], results["new"]
    print(f"sponsors old: {old['sponsors']}")
    print(f"sponsors new: {new['sponsors']}")
    print("\nold blocks:")
    for b in old["blocks"]:
        print(f"  {b[0]:8.1f} - {b[1]:8.1f}  {b[2]}")
    print("\nnew blocks:")
    for b in new["blocks"]:
        print(f"  {b[0]:8.1f} - {b[1]:8.1f}  {b[2]}")

    def overlap(a, b):
        return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))

    dropped, kept, added = [], [], []
    for b in old["blocks"]:
        best = max(new["blocks"], key=lambda n: overlap(b, n),
                   default=None)
        if best and overlap(b, best) > 0.5 * min(b[1] - b[0], best[1] - best[0]):
            kept.append((b, best))
        else:
            dropped.append(b)
    for n in new["blocks"]:
        if not any(n is k[1] for k in kept):
            added.append(n)
    print(f"\nkept {len(kept)} / dropped {len(dropped)} / added {len(added)}")
    for b in dropped:
        print(f"  DROPPED old block {b[0]:.0f}-{b[1]:.0f} ({b[2]}) — "
              "check whether this is a real regression")
    for n in added:
        print(f"  ADDED   new block {n[0]:.0f}-{n[1]:.0f} ({n[2]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
