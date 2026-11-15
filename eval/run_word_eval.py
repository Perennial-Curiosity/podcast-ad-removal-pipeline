#!/usr/bin/env python3
"""Run detect_ad_blocks on BLAB-provided word timestamps and score it.

This is the fast iteration path: BLAB already ships word-level timings
for all 200 advertisement-localization episodes, so we can compare
detector versions without downloading audio or re-transcribing.

Limitations (documented in every report):
- Words are BLAB's alignments, not the pipeline's Whisper 'base' transcript.
- No audio: voice/reverb votes are off, and silences are inferred from
  inter-word gaps (>= 0.8s) rather than ffmpeg silencedetect.
- Sponsors/chapters come from a cached yt-dlp info.json if present;
  otherwise description sponsors are empty (popular-brand / CTA / URL
  methods still run).

Usage:
  python3 eval/run_word_eval.py --pipeline pipeline/run_pipeline.py \\
      --gt eval_work/blab_gt/advertisement_localization.json \\
      --label main --episodes ACZrZ9Bm-8I,1m1ye7pTgs0
  python3 eval/run_word_eval.py --pipeline pipeline/run_pipeline.py --all
"""

import argparse
import importlib.util
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from score_segments import (  # noqa: E402
    load_blab_gt, score_episode, aggregate, format_report, _video_id,
)


def load_pipeline(path):
    path = os.path.abspath(path)
    name = "run_pipeline_" + hex(abs(hash(path)))[2:]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Isolate from a previously imported copy
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    os.environ.pop("HF_HUB_OFFLINE", None)
    return mod


def blab_words(ep):
    words = []
    for w in ep.get("groundtruth", {}).get("word_timestamp") or []:
        token = w.get("word") or w.get("text") or ""
        if not token or "start" not in w or "end" not in w:
            continue
        text = token if token.startswith(" ") else " " + token
        words.append((float(w["start"]), float(w["end"]), text))
    words.sort(key=lambda x: x[0])
    return words


def inferred_silences(words, min_dur=0.8):
    sil = []
    for i in range(1, len(words)):
        gap_s, gap_e = words[i - 1][1], words[i][0]
        if gap_e - gap_s >= min_dur:
            sil.append((gap_s, gap_e))
    return sil


def load_info(info_dir, vid):
    if not info_dir:
        return {}
    for name in (f"{vid}.info.json", f"{vid}.json"):
        p = os.path.join(info_dir, name)
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    return {}


def block_span(b):
    return float(b[0]), float(b[1]), b[2] if len(b) > 2 else "ad"


def detect_one(mod, words, silences, info):
    description = info.get("description") or ""
    sponsors = []
    own_domains = None
    if description or info.get("channel_url") or info.get("uploader_url"):
        try:
            sponsors = mod.extract_sponsors(description)
        except Exception as exc:
            print(f"    extract_sponsors failed: {exc!r}", file=sys.stderr)
        try:
            if hasattr(mod, "extract_own_domains"):
                own_domains = mod.extract_own_domains(
                    description, info.get("channel_url") or info.get("uploader_url"),
                    list(sponsors))
        except Exception as exc:
            print(f"    extract_own_domains failed: {exc!r}", file=sys.stderr)
    chapters = []
    for ch in info.get("chapters") or []:
        try:
            chapters.append((float(ch["start_time"]), ch.get("title", "")))
        except (KeyError, TypeError, ValueError):
            pass
    kwargs = {"mp3_path": None, "chapters": chapters or None}
    # newer signature accepts own_domains
    try:
        blocks, suspects = mod.detect_ad_blocks(
            words, sponsors, silences, own_domains=own_domains, **kwargs)
    except TypeError:
        blocks, suspects = mod.detect_ad_blocks(
            words, sponsors, silences, None, chapters or None)
    return blocks, suspects, sponsors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline", required=True, help="path to run_pipeline.py")
    ap.add_argument("--gt", default=os.path.join(REPO, "eval_work/blab_gt/advertisement_localization.json"))
    ap.add_argument("--info-dir", default=os.path.join(REPO, "eval_work/yt_info"))
    ap.add_argument("--work-dir", default=os.path.join(REPO, "eval_work"))
    ap.add_argument("--episodes", help="comma-separated YouTube video IDs")
    ap.add_argument("--count", type=int, help="seeded sample of episodes with 2-4 GT ads")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--all", action="store_true", help="score every BLAB episode")
    ap.add_argument("--label", default="run")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    with open(args.gt) as f:
        raw = json.load(f)
    by_id = {}
    for ep in raw:
        vid = _video_id(ep.get("video_url"))
        if vid:
            by_id[vid] = ep
    gt = load_blab_gt(args.gt)

    if args.all:
        selected = sorted(gt.keys())
    elif args.episodes:
        selected = [v.strip() for v in args.episodes.split(",") if v.strip()]
    else:
        rng = random.Random(args.seed)
        n = args.count or 12
        good = [v for v in gt if 2 <= len(gt[v]) <= 4]
        selected = rng.sample(good, min(n, len(good)))
    print(f"evaluating {len(selected)} episodes with {args.pipeline}")

    mod = load_pipeline(args.pipeline)
    predictions = {}
    episode_results = []
    for vid in selected:
        ep = by_id.get(vid)
        if not ep:
            print(f"  {vid}: not in BLAB GT; skipping", file=sys.stderr)
            continue
        words = blab_words(ep)
        silences = inferred_silences(words)
        info = load_info(args.info_dir, vid)
        blocks, suspects, sponsors = detect_one(mod, words, silences, info)
        spans = [block_span(b) for b in blocks]
        if not args.quiet:
            print(f"  {vid}: {len(words)} words, {len(sponsors)} sponsors, "
                  f"{len(spans)} blocks: "
                  + ", ".join(f"{s:.0f}-{e:.0f}" for s, e, _ in spans))
        predictions[vid] = {
            "blocks": [[round(s, 2), round(e, 2), l] for s, e, l in spans],
            "suspects": [[round(*block_span(s)[:2], 2), block_span(s)[2]]
                         for s in suspects],
            "n_words": len(words),
            "n_sponsors": len(sponsors),
            "has_description": bool(info.get("description")),
        }
        r = score_episode([(s, e) for s, e, _ in spans], gt[vid])
        r["episode"] = vid
        episode_results.append(r)

    results = {"episodes": episode_results,
               "aggregate": aggregate(episode_results, 0.5),
               "meta": {
                   "pipeline": os.path.abspath(args.pipeline),
                   "transcript_source": "BLAB word_timestamp (not Whisper base)",
                   "audio": False,
                   "silence_source": "inter-word gaps >= 0.8s",
                   "n_with_description": sum(
                       1 for p in predictions.values() if p["has_description"]),
               }}
    report = format_report(results, f"BLAB word-timestamp eval — {args.label}")
    print(report)

    os.makedirs(args.work_dir, exist_ok=True)
    out_pred = os.path.join(args.work_dir, f"predictions_{args.label}.json")
    out_metrics = os.path.join(args.work_dir, f"metrics_{args.label}.json")
    out_md = os.path.join(args.work_dir, f"report_{args.label}.md")
    with open(out_pred, "w") as f:
        json.dump(predictions, f, indent=2)
    with open(out_metrics, "w") as f:
        json.dump(results, f, indent=2)
    with open(out_md, "w") as f:
        f.write(report + "\n")
    print(f"\nwrote {out_pred}\nwrote {out_metrics}\nwrote {out_md}")


if __name__ == "__main__":
    main()
