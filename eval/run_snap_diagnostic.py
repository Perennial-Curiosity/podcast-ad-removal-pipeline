#!/usr/bin/env python3
"""Measure _snap_block_edges on BLAB ads, independent of detection recall.

For each ground-truth ad, build a sloppy initial block (the failure mode
the owner reported: start mid-read, end before the sign-off, or both
edges overshoot) and score the block before vs after snapping.

This uses BLAB word timings and ad timestamps only — no audio, no
YouTube, no sponsor list. It answers: when an ad has already been
found, does snapping improve the edges? It does not measure recall.
"""

import argparse
import random
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from score_segments import (  # noqa: E402
    _video_id, edge_metrics, format_report, aggregate, score_episode,
)
from run_word_eval import blab_words, inferred_silences, load_pipeline  # noqa: E402


def sloppy_mid(gt_s, gt_e, inward=8.0):
    dur = gt_e - gt_s
    trim = min(inward, max(0.0, dur / 4))
    return gt_s + trim, gt_e - trim


def sloppy_overshoot(gt_s, gt_e, outward=10.0, ep_end=None):
    s = max(0.0, gt_s - outward)
    e = gt_e + outward
    if ep_end is not None:
        e = min(e, ep_end)
    return s, e


def _jitter_edge(gt, side, rng, inward, outward, ep_end, dur):
    """Per-edge jitter: inward, outward, or exact (seeded)."""
    choice = rng.choice(("in", "out", "exact"))
    if choice == "exact":
        return gt
    if side == "start":
        if choice == "in":
            return gt + min(inward, max(0.0, dur / 4))
        return max(0.0, gt - outward)
    # end
    if choice == "in":
        return gt - min(inward, max(0.0, dur / 4))
    e = gt + outward
    return min(e, ep_end) if ep_end is not None else e


def sloppy_mixed(gt_s, gt_e, rng, inward=8.0, outward=10.0, ep_end=None):
    dur = gt_e - gt_s
    return (
        _jitter_edge(gt_s, "start", rng, inward, outward, ep_end, dur),
        _jitter_edge(gt_e, "end", rng, inward, outward, ep_end, dur),
    )


def score_pairs(preds, gts, episode="x"):
    r = score_episode(preds, gts)
    r["episode"] = episode
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline", required=True)
    ap.add_argument("--gt", default=os.path.join(REPO, "eval_work/blab_gt/advertisement_localization.json"))
    ap.add_argument("--work-dir", default=os.path.join(REPO, "eval_work"))
    ap.add_argument("--label", default="snap-diag")
    ap.add_argument("--inward", type=float, default=8.0)
    ap.add_argument("--outward", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=42,
                    help="RNG seed for the mixed per-edge jitter condition")
    args = ap.parse_args()

    if not hasattr(load_pipeline(args.pipeline), "_snap_block_edges"):
        # load once inside check - reload properly
        pass
    mod = load_pipeline(args.pipeline)
    if not hasattr(mod, "_snap_block_edges"):
        raise SystemExit(f"{args.pipeline} has no _snap_block_edges")
    # Snap logs every moved edge; mute them for the 579-ad sweep.
    if hasattr(mod, "log"):
        mod.log = lambda *a, **k: None

    with open(args.gt) as f:
        raw = json.load(f)

    conditions = {
        "mid_read": [],      # (pred_before, pred_after, gt, vid)
        "overshoot": [],
        "mixed": [],
    }
    n_ads = 0
    n_skipped = 0
    for ep in raw:
        vid = _video_id(ep.get("video_url"))
        ads = []
        for s in ep.get("groundtruth", {}).get("ads_segment", []):
            if "start" in s and "end" in s and s["end"] > s["start"]:
                ads.append((float(s["start"]), float(s["end"])))
        if not vid or not ads:
            continue
        words = blab_words(ep)
        if len(words) < 8:
            n_skipped += len(ads)
            continue
        nw = mod.norm_words(words)
        silences = inferred_silences(words)
        ep_end = words[-1][1]
        n_ads += len(ads)
        builders = [
            ("mid_read", lambda s, e, i: sloppy_mid(s, e, args.inward)),
            ("overshoot", lambda s, e, i: sloppy_overshoot(s, e, args.outward, ep_end)),
            ("mixed", lambda s, e, i: sloppy_mixed(
                s, e, random.Random(f"{args.seed}|{vid}|{i}|{round(s, 3)}|{round(e, 3)}"),
                args.inward, args.outward, ep_end)),
        ]
        for cond, builder in builders:
            before = [(builder(s, e, i)[0], builder(s, e, i)[1], "x:ad")
                      for i, (s, e) in enumerate(ads)]
            after = mod._snap_block_edges(words, nw, silences, before, [])
            for b, a, g in zip(before, after, ads):
                conditions[cond].append((b, a, g, vid))

    reports = {}
    for cond, rows in conditions.items():
        # Pair each sloppy/snapped block with the SAME ground-truth ad.
        # Do not greedy-rematch across the pool — ads from different
        # episodes often share clock times near 0s.
        before_pairs = [((b[0], b[1]), g, None) for b, a, g, vid in rows]
        after_pairs = [((a[0], a[1]), g, None) for b, a, g, vid in rows]
        be = edge_metrics(before_pairs)
        ae = edge_metrics(after_pairs)
        reports[cond] = {
            "n": len(rows),
            "before_edges": be,
            "after_edges": ae,
        }
        better = worse = same = 0
        moved = 0
        worst_after = []
        for b, a, g, vid in rows:
            for side in (0, 1):
                eb = abs((b[0] if side == 0 else b[1]) - (g[0] if side == 0 else g[1]))
                ea = abs((a[0] if side == 0 else a[1]) - (g[0] if side == 0 else g[1]))
                if abs((a[side] - b[side])) >= 0.05:
                    moved += 1
                if ea < eb - 0.05:
                    better += 1
                elif ea > eb + 0.05:
                    worse += 1
                else:
                    same += 1
                if ea >= 20:
                    worst_after.append((ea, vid, "start" if side == 0 else "end",
                                        g, (b[0], b[1]), (a[0], a[1])))
        worst_after.sort(reverse=True)
        reports[cond]["edge_deltas"] = {
            "better": better, "worse": worse, "same": same, "moved": moved,
            "worst_after": [
                {"abs_err": round(w[0], 2), "vid": w[1], "side": w[2],
                 "gt": [round(w[3][0], 2), round(w[3][1], 2)],
                 "before": [round(w[4][0], 2), round(w[4][1], 2)],
                 "after": [round(w[5][0], 2), round(w[5][1], 2)]}
                for w in worst_after[:8]
            ],
        }

    def fmt_edges(e):
        def _s(v):
            return "n/a" if v is None else f"{v:.2f}s"
        def _p(v):
            return "n/a" if v is None else f"{100*v:.1f}%"
        return (f"mean {_s(e.get('mean_abs'))} (start {_s(e.get('mean_abs_start'))}, "
                f"end {_s(e.get('mean_abs_end'))}); median {_s(e.get('median_abs'))}; "
                f"within 1/2/5s {_p(e.get('within_1s'))}/{_p(e.get('within_2s'))}/{_p(e.get('within_5s'))}; "
                f"worst {_s(e.get('max_abs'))}")

    lines = [
        f"# Snap diagnostic — {args.label}",
        "",
        f"Ads considered: {n_ads}  (skipped {n_skipped} for short transcripts)",
        f"Pipeline: {os.path.abspath(args.pipeline)}",
        f"Transcript: BLAB word_timestamp. Silences: inter-word gaps >= 0.8s.",
        f"Sponsors: none (snap uses cues, sign-offs, and pauses only).",
        f"Sloppy mid-read inward={args.inward}s; overshoot outward={args.outward}s; "
        f"mixed per-edge jitter seed={args.seed}.",
        "",
    ]
    for cond, rep in reports.items():
        d = rep["edge_deltas"]
        lines += [
            f"## Condition: {cond} (n={rep['n']} ads, {rep['n']*2} edges)",
            "",
            f"- Before: {fmt_edges(rep['before_edges'])}",
            f"- After:  {fmt_edges(rep['after_edges'])}",
            f"- Edges better / worse / same: {d['better']} / {d['worse']} / {d['same']}  "
            f"(moved {d['moved']})",
            "",
        ]

    report = "\n".join(lines)
    print(report)
    os.makedirs(args.work_dir, exist_ok=True)
    out_md = os.path.join(args.work_dir, f"report_{args.label}.md")
    out_json = os.path.join(args.work_dir, f"metrics_{args.label}.json")
    # drop huge before/after episode dumps from json? keep edges summaries
    slim = {k: {
        "n": v["n"],
        "before_edges": {kk: vv for kk, vv in v["before_edges"].items() if kk != "edges"},
        "after_edges": {kk: vv for kk, vv in v["after_edges"].items() if kk != "edges"},
        "edge_deltas": v["edge_deltas"],
    } for k, v in reports.items()}
    with open(out_md, "w") as f:
        f.write(report + "\n")
    with open(out_json, "w") as f:
        json.dump({"meta": {"pipeline": args.pipeline, "n_ads": n_ads,
                            "skipped": n_skipped, "inward": args.inward,
                            "outward": args.outward, "seed": args.seed},
                   "conditions": slim}, f, indent=2)
    print(f"wrote {out_md}\nwrote {out_json}")


if __name__ == "__main__":
    main()
