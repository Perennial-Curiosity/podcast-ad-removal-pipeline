#!/usr/bin/env python3
"""Segment-overlap and boundary-error scoring for the podcast ad detector.

Compares detector output (predicted ad blocks) against human-verified
ground truth and reports three complementary views:

1. Time-based precision / recall (interval overlap, per-second equivalent).
2. Segment-based precision / recall / F1 at an IoU threshold (default 0.5),
   greedy one-to-one matching.
3. Coverage-based matching (TESTS.md): a prediction matches a label if
   overlap is >= 50% of the label duration. Boundary error (mean/median
   absolute start and end error, share of edges within 1s/2s/5s) is
   computed on coverage-matched pairs only.

Ground truth sources (see eval/README.md):
- BLAB advertisement localization JSON
- PodRanker-style reports
- Generic {episode_key: [[start, end], ...]} JSON
"""

import json
import os
import re
import sys


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
    """Load PodRanker-style ad-study reports -> {episode_key: [(start, end)]}."""
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


def load_generic_gt(path):
    """Load a simple {episode_key: [[start, end], ...] or {blocks: ...}} JSON."""
    with open(path) as f:
        data = json.load(f)
    out = {}
    if isinstance(data, list):
        return load_blab_gt(path)
    for k, v in data.items():
        if k in ("episodes", "aggregate", "meta"):
            continue
        if isinstance(v, dict):
            segs = v.get("ads") or v.get("blocks") or v.get("segments") or v.get("gt") or []
        else:
            segs = v
        out[str(k)] = [(float(b[0]), float(b[1])) for b in segs]
    return out


def load_gt(path, kind="auto"):
    """kind: auto, blab, podranker, generic."""
    if kind == "podranker":
        return load_podranker_gt(path)
    if kind == "blab":
        return load_blab_gt(path)
    if kind == "generic":
        return load_generic_gt(path)
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return load_blab_gt(path)
    return load_generic_gt(path)


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
    """Greedy one-to-one IoU matching. Returns list of (pred, gt, iou)."""
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


def match_segments_coverage(preds, gt, coverage_threshold=0.5):
    """Greedy one-to-one matching by overlap / GT duration (TESTS.md rule)."""
    pairs = []
    for i, p in enumerate(preds):
        for k, g in enumerate(gt):
            dur = g[1] - g[0]
            if dur <= 0:
                continue
            cov = _overlap(p, g) / dur
            if cov >= coverage_threshold:
                pairs.append((cov, i, k, p, g))
    pairs.sort(reverse=True)
    used_p, used_g, matches = set(), set(), []
    for v, i, k, p, g in pairs:
        if i in used_p or k in used_g:
            continue
        used_p.add(i)
        used_g.add(k)
        matches.append((p, g, v))
    return matches


def _median(xs):
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    if n % 2:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) / 2


def edge_metrics(matches):
    """Boundary error on matched (pred, gt, score) pairs.

    start_err / end_err are pred - gt (positive = late). Unmatched ads
    are excluded — they are recall misses, not edge error.
    """
    start_errs, end_errs = [], []
    details = []
    for item in matches:
        p, g = item[0], item[1]
        se = p[0] - g[0]
        ee = p[1] - g[1]
        start_errs.append(se)
        end_errs.append(ee)
        details.append({
            "pred": [round(p[0], 3), round(p[1], 3)],
            "gt": [round(g[0], 3), round(g[1], 3)],
            "start_err": round(se, 3),
            "end_err": round(ee, 3),
            "abs_start": round(abs(se), 3),
            "abs_end": round(abs(ee), 3),
        })
    abs_start = [abs(x) for x in start_errs]
    abs_end = [abs(x) for x in end_errs]
    abs_all = abs_start + abs_end

    def _share(xs, t):
        return (sum(1 for x in xs if x <= t) / len(xs)) if xs else None

    def _mean(xs):
        return (sum(xs) / len(xs)) if xs else None

    return {
        "n_edges": len(abs_all),
        "n_matched": len(matches),
        "mean_abs_start": _mean(abs_start),
        "mean_abs_end": _mean(abs_end),
        "mean_abs": _mean(abs_all),
        "median_abs_start": _median(abs_start),
        "median_abs_end": _median(abs_end),
        "median_abs": _median(abs_all),
        "signed_mean_start": _mean(start_errs),
        "signed_mean_end": _mean(end_errs),
        "within_1s": _share(abs_all, 1.0),
        "within_2s": _share(abs_all, 2.0),
        "within_5s": _share(abs_all, 5.0),
        "max_abs": max(abs_all) if abs_all else None,
        "edges": details,
    }


def score_episode(preds, gt, iou_threshold=0.5, coverage_threshold=0.5):
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
    cov_matches = match_segments_coverage(preds, gt, coverage_threshold)
    cov_tp = len(cov_matches)
    cov_prec = cov_tp / len(preds) if preds else None
    cov_rec = cov_tp / len(gt) if gt else None
    cov_f1 = None
    if cov_prec is not None and cov_rec is not None and cov_prec + cov_rec > 0:
        cov_f1 = 2 * cov_prec * cov_rec / (cov_prec + cov_rec)
    edges = edge_metrics(cov_matches if cov_matches else matches)
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
        "coverage": {
            "threshold": coverage_threshold,
            "tp": cov_tp,
            "precision": cov_prec,
            "recall": cov_rec,
            "f1": cov_f1,
            "false_positives": [list(p) for p in preds
                                if not any(p == m[0] for m in cov_matches)],
            "misses": [list(g) for g in gt
                       if not any(g == m[1] for m in cov_matches)],
        },
        "edges": edges,
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
    cov_tp = sum(r.get("coverage", {}).get("tp", 0) for r in episode_results)
    cov_prec = cov_tp / npred if npred else None
    cov_rec = cov_tp / ngt if ngt else None
    cov_f1 = None
    if cov_prec is not None and cov_rec is not None and cov_prec + cov_rec > 0:
        cov_f1 = 2 * cov_prec * cov_rec / (cov_prec + cov_rec)
    pooled = []
    for r in episode_results:
        for e in r.get("edges", {}).get("edges", []):
            pooled.append(((e["pred"][0], e["pred"][1]),
                           (e["gt"][0], e["gt"][1]), None))
    edges = edge_metrics(pooled)
    return {
        "iou_threshold": iou_threshold,
        "episodes": len(episode_results),
        "segment": {"tp": tp, "fp": npred - tp, "fn": ngt - tp,
                    "precision": precision, "recall": recall, "f1": f1},
        "coverage": {"tp": cov_tp, "fp": npred - cov_tp, "fn": ngt - cov_tp,
                     "precision": cov_prec, "recall": cov_rec, "f1": cov_f1},
        "time_based": {"pred_time": pt, "gt_time": gtt, "overlap_time": o,
                       "precision": tprec, "recall": trec, "f1": tf1},
        "edges": edges,
    }


def _pct(v):
    return "n/a" if v is None else f"{100 * v:.1f}%"


def _sec(v):
    return "n/a" if v is None else f"{v:.2f}s"


def format_report(results, title="Evaluation"):
    """Render a results dict (aggregate + per-episode) as markdown."""
    lines = [f"# {title}", ""]
    agg = results["aggregate"]
    s, t = agg["segment"], agg["time_based"]
    cov = agg.get("coverage") or {}
    e = agg.get("edges") or {}
    lines.append(f"Episodes: {agg['episodes']}  |  IoU threshold: {agg['iou_threshold']}")
    lines.append("")
    lines.append("| Metric | Segment (IoU) | Coverage (>=50% of GT) | Time-based |")
    lines.append("|---|---|---|---|")
    lines.append(f"| Precision | {_pct(s['precision'])} | {_pct(cov.get('precision'))} | {_pct(t['precision'])} |")
    lines.append(f"| Recall | {_pct(s['recall'])} | {_pct(cov.get('recall'))} | {_pct(t['recall'])} |")
    lines.append(f"| F1 | {_pct(s['f1'])} | {_pct(cov.get('f1'))} | {_pct(t['f1'])} |")
    lines.append(
        f"| Counts | TP {s['tp']} / FP {s['fp']} / FN {s['fn']} | "
        f"TP {cov.get('tp', 'n/a')} / FP {cov.get('fp', 'n/a')} / FN {cov.get('fn', 'n/a')} | "
        f"{t['gt_time']:.0f}s GT ad time, {t['pred_time']:.0f}s predicted |"
    )
    lines.append("")
    lines.append("## Boundary error (matched ads only)")
    lines.append("")
    lines.append(f"- Matched edges: {e.get('n_edges', 0)}")
    lines.append(
        f"- Mean |start| / |end| / all: {_sec(e.get('mean_abs_start'))} / "
        f"{_sec(e.get('mean_abs_end'))} / {_sec(e.get('mean_abs'))}"
    )
    lines.append(
        f"- Median |start| / |end| / all: {_sec(e.get('median_abs_start'))} / "
        f"{_sec(e.get('median_abs_end'))} / {_sec(e.get('median_abs'))}"
    )
    lines.append(
        f"- Within 1s / 2s / 5s: {_pct(e.get('within_1s'))} / "
        f"{_pct(e.get('within_2s'))} / {_pct(e.get('within_5s'))}"
    )
    lines.append(
        f"- Signed mean start / end (late +): {_sec(e.get('signed_mean_start'))} / "
        f"{_sec(e.get('signed_mean_end'))}"
    )
    lines.append(f"- Worst |edge|: {_sec(e.get('max_abs'))}")
    lines.append("")
    lines.append("## Per episode")
    lines.append("")
    lines.append("| Episode | GT | Pred | Cov R | Seg F1 | Mean |err| | Med |err| | <=2s | <=5s |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results["episodes"]:
        ed = r.get("edges") or {}
        cr = (r.get("coverage") or {}).get("recall")
        lines.append(
            f"| {r['episode']} | {r['n_gt']} | {r['n_pred']} | "
            f"{_pct(cr)} | {_pct(r['segment_f1'])} | "
            f"{_sec(ed.get('mean_abs'))} | {_sec(ed.get('median_abs'))} | "
            f"{_pct(ed.get('within_2s'))} | {_pct(ed.get('within_5s'))} |"
        )
    lines.append("")
    for r in results["episodes"]:
        if r["misses"] or r["false_positives"] or r.get("edges", {}).get("edges"):
            lines.append(f"### {r['episode']}")
            for ev in r.get("edges", {}).get("edges", []):
                lines.append(
                    f"- MATCH GT {ev['gt'][0]:.1f}-{ev['gt'][1]:.1f} vs "
                    f"pred {ev['pred'][0]:.1f}-{ev['pred'][1]:.1f}  "
                    f"start {ev['start_err']:+.1f}s  end {ev['end_err']:+.1f}s"
                )
            for m in r["misses"]:
                lines.append(
                    f"- MISS {m[0]:.0f}s-{m[1]:.0f}s "
                    f"({m[1] - m[0]:.0f}s) — uncovered ground truth"
                )
            for pred in r["false_positives"]:
                lines.append(
                    f"- FP {pred[0]:.0f}s-{pred[1]:.0f}s "
                    f"({pred[1] - pred[0]:.0f}s) — predicted, no GT overlap >= IoU"
                )
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Score predicted ad segments against ground truth")
    ap.add_argument("groundtruth",
                    help="BLAB advertisement_localization.json, PodRanker report "
                         "dir/file, or generic {episode: [[start,end],...]} JSON")
    ap.add_argument("--predictions", required=True,
                    help="JSON: {episode_key: [[start, end], ...]} or "
                         "{episode_key: {blocks: [{start,end,...}]}}")
    ap.add_argument("--podranker", action="store_true",
                    help="treat groundtruth as PodRanker-style reports")
    ap.add_argument("--generic", action="store_true",
                    help="treat groundtruth as {episode: [[start,end],...]} JSON")
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("-o", "--out", help="write JSON metrics here")
    args = ap.parse_args()

    kind = "podranker" if args.podranker else ("generic" if args.generic else "auto")
    gt = load_gt(args.groundtruth, kind)
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
        print(f"wrote {args.out}", file=sys.stderr)
