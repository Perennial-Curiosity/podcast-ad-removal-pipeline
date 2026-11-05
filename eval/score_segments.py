#!/usr/bin/env python3
"""Segment-overlap scoring for the podcast ad detector.

Compares detector output (predicted ad blocks) against human-verified
ground truth and reports two complementary views:

1. Time-based precision / recall (per-second framing, computed on
   intervals): what fraction of predicted ad time really is an ad, and
   what fraction of real ad time was covered. Robust to boundary jitter.
2. Segment-based precision / recall / F1 at an IoU threshold: each
   ground-truth ad can be matched by at most one predicted block
   (greedy best-IoU matching). This is the strict "did you find the ad"
   view; it punishes split and merged blocks.

Ground truth sources (see eval/README.md):
- BLAB (Brutally Long Audio Bench) advertisement localization:
  200 YouTube podcast episodes, human-verified ad segment timestamps.
- PodRanker ad study reports (128 episodes, timestamped ad blocks),
  if you have obtained the JSON reports from PodRanker.

Usage as a library:

    from score_segments import load_blab_gt, load_podranker_gt, score_episode
    gt = load_blab_gt("advertisement_localization.json")   # {video_id: [(s,e),...]}
    m = score_episode([(10, 60)], [(12, 55)], iou_threshold=0.5)
"""

import json
import os
import re
import sys


# ----------------------------------------------------------------------
# Ground-truth loaders
# ----------------------------------------------------------------------

def _video_id(url):
    m = re.search(r"[?&]v=([\w-]{6,})", url or "")
    return m.group(1) if m else None


def load_blab_gt(path):
    """Load BLAB advertisement_localization.json -> {video_id: [(start, end)]}."""
    with open(path) as f:
        data = json.load(f)
    out = {}
    for ep in data:
        vid = _video_id(ep.get("video_url"))
        segs = []
        for s in ep.get("groundtruth", {}).get("ads_segment", []):
            if "start" in s and "end" in s:
                segs.append((float(s["start"]), float(s["end"])))
        if vid:
            out[vid] = segs
    return out


def _parse_time(v):
    """Accept seconds (int/float/str) or 'mm:ss' / 'hh:mm:ss' strings."""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    v = v.strip()
    if re.fullmatch(r"[\d.]+", v):
        return float(v)
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)", v)
    if m:
        h = int(m.group(1) or 0)
        return h * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return None


def load_podranker_gt(path):
    """Load PodRanker-style ad-study reports -> {episode_key: [(start, end)]}.

    PodRanker's blog publishes (per episode) JSON reports with timestamped
    ad blocks. The exact schema is not publicly documented, so this loader
    accepts a directory of per-episode JSON files (or a single file that is
    a list of reports / a mapping) and looks for ad segments under the
    common key spellings, accepting seconds or 'mm:ss' timestamps:

        {"episode": "...", "ad_blocks": [{"start": 63.2, "end": 95.4, ...}]}
    """
    reports = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.endswith(".json"):
                with open(os.path.join(path, name)) as f:
                    reports.append((name, json.load(f)))
    else:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            reports = [(f"report_{i}", r) for i, r in enumerate(data)]
        else:
            reports = [("report", data)]

    segment_keys = ("ad_blocks", "ads", "ad_segments", "adblocks", "segments")
    out = {}
    for key, rep in reports:
        segs = []
        candidates = None
        if isinstance(rep, dict):
            for k in segment_keys:
                if isinstance(rep.get(k), list):
                    candidates = rep[k]
                    break
        elif isinstance(rep, list):
            candidates = rep
        if isinstance(candidates, list):
            for s in candidates:
                if isinstance(s, dict):
                    st = s.get("start", s.get("start_time", s.get("from")))
                    en = s.get("end", s.get("end_time", s.get("to")))
                elif isinstance(s, (list, tuple)) and len(s) >= 2:
                    st, en = s[0], s[1]
                else:
                    continue
                st, en = _parse_time(st), _parse_time(en)
                if st is not None and en is not None and en > st:
                    segs.append((st, en))
        name = key
        if isinstance(rep, dict):
            name = rep.get("episode") or rep.get("title") or key
        out[str(name)] = segs
    return out


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------

def _overlap(a, b):
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _iou(a, b):
    inter = _overlap(a, b)
    if inter <= 0:
        return 0.0
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union > 0 else 0.0


def time_based_metrics(preds, gt):
    """Interval-based precision/recall (equivalent to per-second scoring)."""
    if not preds and not gt:
        return {"pred_time": 0.0, "gt_time": 0.0, "overlap_time": 0.0,
                "precision": None, "recall": None, "f1": None}
    overlap = 0.0
    j = 0
    gt_sorted = sorted(gt)
    for p in sorted(preds):
        while j < len(gt_sorted) and gt_sorted[j][1] <= p[0]:
            j += 1
        for g in gt_sorted[j:]:
            if g[0] >= p[1]:
                break
            overlap += _overlap(p, g)
    pred_time = sum(e - s for s, e in preds)
    gt_time = sum(e - s for s, e in gt)
    precision = overlap / pred_time if pred_time > 0 else None
    recall = overlap / gt_time if gt_time > 0 else None
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = 2 * precision * recall / (precision + recall)
    return {"pred_time": pred_time, "gt_time": gt_time, "overlap_time": overlap,
            "precision": precision, "recall": recall, "f1": f1}


def match_segments(preds, gt, iou_threshold=0.5):
    """Greedy one-to-one IoU matching. Returns list of (pred, gt, iou) matches."""
    pairs = []
    for i, p in enumerate(preds):
        for k, g in enumerate(gt):
            v = _iou(p, g)
            if v >= iou_threshold:
                pairs.append((v, i, k, p, g))
    pairs.sort(reverse=True)
    used_p, used_g, matches = set(), set(), []
    for v, i, k, p, g in pairs:
        if i in used_p or k in used_g:
            continue
        used_p.add(i)
        used_g.add(k)
        matches.append((p, g, v))
    return matches


def score_episode(preds, gt, iou_threshold=0.5):
    """Full metrics for one episode. preds/gt are lists of (start, end)."""
    preds = [(float(s), float(e)) for s, e in preds]
    gt = [(float(s), float(e)) for s, e in gt]
    matches = match_segments(preds, gt, iou_threshold)
    tp = len(matches)
    precision = tp / len(preds) if preds else None
    recall = tp / len(gt) if gt else None
    f1 = None
    if precision is not None and recall is not None and precision + recall > 0:
        f1 = 2 * precision * recall / (precision + recall)
    tb = time_based_metrics(preds, gt)
    return {
        "n_pred": len(preds), "n_gt": len(gt), "tp": tp,
        "segment_precision": precision, "segment_recall": recall,
        "segment_f1": f1,
        "matches": [{"pred": list(p), "gt": list(g), "iou": round(v, 3)}
                    for p, g, v in matches],
        "false_positives": [list(p) for p in preds
                            if not any(p == m[0] for m in matches)],
        "misses": [list(g) for g in gt
                   if not any(g == m[1] for m in matches)],
        "time_based": tb,
    }


def aggregate(episode_results, iou_threshold=0.5):
    """Micro-average over episodes (pooled TP/FP/FN for segment metrics)."""
    tp = sum(r["tp"] for r in episode_results)
    npred = sum(r["n_pred"] for r in episode_results)
    ngt = sum(r["n_gt"] for r in episode_results)
    precision = tp / npred if npred else None
    recall = tp / ngt if ngt else None
    f1 = None
    if precision is not None and recall is not None and precision + recall > 0:
        f1 = 2 * precision * recall / (precision + recall)
    o = sum(r["time_based"]["overlap_time"] for r in episode_results)
    pt = sum(r["time_based"]["pred_time"] for r in episode_results)
    gtt = sum(r["time_based"]["gt_time"] for r in episode_results)
    tprec = o / pt if pt else None
    trec = o / gtt if gtt else None
    tf1 = None
    if tprec is not None and trec is not None and tprec + trec > 0:
        tf1 = 2 * tprec * trec / (tprec + trec)
    return {
        "iou_threshold": iou_threshold,
        "episodes": len(episode_results),
        "segment": {"tp": tp, "fp": npred - tp, "fn": ngt - tp,
                   "precision": precision, "recall": recall, "f1": f1},
        "time_based": {"pred_time": pt, "gt_time": gtt, "overlap_time": o,
                       "precision": tprec, "recall": trec, "f1": tf1},
    }


def format_report(results, title="Evaluation"):
    """Render a results dict (aggregate + per-episode) as markdown."""
    lines = [f"# {title}", ""]
    agg = results["aggregate"]
    s, t = agg["segment"], agg["time_based"]
    lines.append(f"Episodes: {agg['episodes']}  |  IoU threshold: {agg['iou_threshold']}")
    lines.append("")
    lines.append("| Metric | Segment-based | Time-based |")
    lines.append("|---|---|---|")
    fmt = lambda v: "n/a" if v is None else f"{100 * v:.1f}%"
    lines.append(f"| Precision | {fmt(s['precision'])} | {fmt(t['precision'])} |")
    lines.append(f"| Recall | {fmt(s['recall'])} | {fmt(t['recall'])} |")
    lines.append(f"| F1 | {fmt(s['f1'])} | {fmt(t['f1'])} |")
    lines.append(f"| Counts | TP {s['tp']} / FP {s['fp']} / FN {s['fn']} | "
                 f"{t['gt_time']:.0f}s GT ad time, {t['pred_time']:.0f}s predicted |")
    lines.append("")
    lines.append("## Per episode")
    lines.append("")
    lines.append("| Episode | GT ads | Predicted | Seg P | Seg R | Time P | Time R |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results["episodes"]:
        lines.append(
            f"| {r['episode']} | {r['n_gt']} | {r['n_pred']} | "
            f"{fmt(r['segment_precision'])} | {fmt(r['segment_recall'])} | "
            f"{fmt(r['time_based']['precision'])} | {fmt(r['time_based']['recall'])} |")
    lines.append("")
    for r in results["episodes"]:
        if r["misses"] or r["false_positives"]:
            lines.append(f"### {r['episode']}")
            for m in r["misses"]:
                lines.append(f"- MISS {m[0]:.0f}s-{m[1]:.0f}s "
                            f"({m[1] - m[0]:.0f}s) — uncovered ground truth")
            for p in r["false_positives"]:
                lines.append(f"- FP {p[0]:.0f}s-{p[1]:.0f}s "
                            f"({p[1] - p[0]:.0f}s) — predicted, no GT overlap >= IoU")
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Score predicted ad segments against ground truth")
    ap.add_argument("groundtruth",
                   help="BLAB advertisement_localization.json, PodRanker report "
                        "dir/file, or a predictions JSON to self-describe")
    ap.add_argument("--predictions", required=True,
                    help="JSON: {episode_key: [[start, end], ...]} or "
                         "{episode_key: {blocks: [{start,end,...}]}}")
    ap.add_argument("--podranker", action="store_true",
                    help="treat groundtruth as PodRanker-style reports")
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("-o", "--out", help="write JSON metrics here")
    args = ap.parse_args()

    gt = (load_podranker_gt(args.groundtruth) if args.podranker
          else load_blab_gt(args.groundtruth))
    with open(args.predictions) as f:
        preds_raw = json.load(f)
    preds = {}
    for k, v in preds_raw.items():
        if isinstance(v, dict):
            v = v.get("blocks", v.get("segments", []))
        preds[k] = [(float(b[0]), float(b[1])) for b in v]

    results = {"episodes": [], "aggregate": None}
    for vid, segs in gt.items():
        if vid not in preds:
            continue
        r = score_episode(preds.get(vid, []), segs, args.iou)
        r["episode"] = vid
        results["episodes"].append(r)
    results["aggregate"] = aggregate(results["episodes"], args.iou)
    print(format_report(results))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nwrote {args.out}", file=sys.stderr)
