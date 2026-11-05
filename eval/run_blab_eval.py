#!/usr/bin/env python3
"""Run the podcast ad detector on BLAB episodes and score it.

BLAB (Brutally Long Audio Bench, https://arxiv.org/abs/2505.03054)
contains an Advertisement Localization task: 200 full-length YouTube
podcast episodes with human-verified ad segment timestamps. This script

  1. downloads the BLAB annotations (advertisement_localization.json),
  2. downloads episode audio + metadata (title, description, chapters)
     with yt-dlp,
  3. transcribes with faster-whisper (the pipeline's own chunked
     transcription, cached and resumable),
  4. runs pipeline/run_pipeline.py's detect_ad_blocks on the episode,
  5. scores the detected blocks against the BLAB ground truth with
     eval/score_segments.py.

Audio, transcripts and predictions are cached under --work-dir, so you can
re-run detection (e.g. after a code change) with --skip-download and only
transcription gaps are filled.

Usage:
  python3 eval/run_blab_eval.py --count 12            # random sample
  python3 eval/run_blab_eval.py --episodes VID1,VID2  # specific episodes
  python3 eval/run_blab_eval.py --episodes VID1 --skip-download  # re-detect
  python3 eval/run_blab_eval.py ... --proxy http://127.0.0.1:18080

The --proxy flag is for environments where YouTube blocks direct
downloads (it passes the flag through to yt-dlp).
"""

import argparse
import importlib.util
import json
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PIPELINE_DIR = os.path.join(REPO, "pipeline")
GT_DIR = os.path.join(os.path.dirname(REPO), "groundtruth")


def load_pipeline():
    """Import pipeline/run_pipeline.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "run_pipeline", os.path.join(PIPELINE_DIR, "run_pipeline.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_pipeline"] = mod
    spec.loader.exec_module(mod)
    # BLAB runs need the whisper model downloadable; the pipeline sets
    # HF_HUB_OFFLINE=1 at import for its own broken-proxy environment.
    os.environ.pop("HF_HUB_OFFLINE", None)
    return mod


def fetch_blab_annotations(gt_dir):
    path = os.path.join(gt_dir, "blab", "blab_long_audio",
                         "advertisement_localization.json")
    if os.path.exists(path):
        return path
    try:
        from huggingface_hub import hf_hub_download
        hf_hub_download("oreva/blab_long_audio",
                       "blab_long_audio/advertisement_localization.json",
                       repo_type="dataset", local_dir=os.path.join(gt_dir, "blab"))
        return path
    except Exception as e:
        raise SystemExit(
            f"could not fetch BLAB annotations ({e}); download "
            "https://huggingface.co/datasets/oreva/blab_long_audio "
            "manually and pass --gt")


def download_episode(vid, ep_dir, proxy=None):
    """yt-dlp the audio + info json. Returns (audio_path, info) or (None, None)."""
    url = f"https://www.youtube.com/watch?v={vid}"
    info_path = os.path.join(ep_dir, f"{vid}.info.json")
    if os.path.exists(info_path):
        with open(info_path) as f:
            info = json.load(f)
        for cand in os.listdir(ep_dir):
            if cand.startswith(vid + ".") and not cand.endswith(".json"):
                return os.path.join(ep_dir, cand), info
    base = ["yt-dlp", "--no-playlist", "--no-part", "--write-info-json",
            "-f", "ba/ba[ext=m4a]/ba[ext=webm]",
            "-o", os.path.join(ep_dir, f"{vid}.%(ext)s")]
    if proxy:
        base += ["--proxy", proxy]
    # tv_embedded returns direct progressive audio URLs that still work
    # from restricted environments; fall back to the default client.
    for extra in (["--extractor-args", "youtube:player_client=tv_embedded"], []):
        r = subprocess.run(base + extra + [url], capture_output=True, text=True)
        if r.returncode == 0:
            if os.path.exists(info_path):
                with open(info_path) as f:
                    info = json.load(f)
                for cand in os.listdir(ep_dir):
                    if cand.startswith(vid + ".") and not cand.endswith(".json"):
                        return os.path.join(ep_dir, cand), info
    print(r.stderr[-500:] if r.stderr else "download failed", file=sys.stderr)
    return None, None


def detect_episode(mod, audio_path, info, work_dir):
    """Transcribe + run detect_ad_blocks. Returns (blocks, suspects, words)."""
    transcript_dir = os.path.join(work_dir, "transcript")
    words, duration = mod.transcribe_chunked(audio_path, transcript_dir)
    description = info.get("description") or ""
    sponsors = mod.extract_sponsors(description)
    silences = mod.find_silences(audio_path, work_dir, min_dur=0.8)
    chapters = []
    for ch in info.get("chapters") or []:
        try:
            chapters.append((float(ch["start_time"]), ch.get("title", "")))
        except (KeyError, TypeError, ValueError):
            pass
    blocks, suspects = mod.detect_ad_blocks(words, sponsors, silences,
                                            audio_path, chapters)
    return blocks, suspects, words, sponsors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", help="path to advertisement_localization.json")
    ap.add_argument("--count", type=int, default=12,
                   help="random sample size (seeded, reproducible)")
    ap.add_argument("--episodes", help="comma-separated BLAB video IDs")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--work-dir", default=os.path.join(REPO, "eval_work"))
    ap.add_argument("--proxy", help="yt-dlp --proxy value")
    ap.add_argument("--skip-download", action="store_true",
                   help="only re-run detection on cached episodes")
    ap.add_argument("--label", default="run",
                    help="label for the predictions file (predictions_<label>.json)")
    args = ap.parse_args()

    sys.path.insert(0, HERE)
    from score_segments import load_blab_gt, score_episode, aggregate, format_report

    gt_path = args.gt or fetch_blab_annotations(GT_DIR)
    gt = load_blab_gt(gt_path)
    all_ids = sorted(gt.keys())
    if args.episodes:
        selected = [v.strip() for v in args.episodes.split(",") if v.strip()]
    else:
        rng = random.Random(args.seed)
        # prefer episodes with 2-4 GT ads (enough signal, manageable runtime)
        good = [v for v in all_ids if 2 <= len(gt[v]) <= 4]
        selected = rng.sample(good, min(args.count, len(good)))
    print(f"evaluating {len(selected)} episodes: {', '.join(selected)}")

    mod = load_pipeline()
    os.makedirs(args.work_dir, exist_ok=True)

    predictions = {}
    episode_results = []
    for vid in selected:
        ep_dir = os.path.join(args.work_dir, "blab", vid)
        os.makedirs(ep_dir, exist_ok=True)
        if args.skip_download:
            audio_path = info = None
            for cand in os.listdir(ep_dir):
                if cand.startswith(vid + ".") and not cand.endswith(".json"):
                    audio_path = os.path.join(ep_dir, cand)
            ipath = os.path.join(ep_dir, f"{vid}.info.json")
            if os.path.exists(ipath):
                with open(ipath) as f:
                    info = json.load(f)
            if not audio_path or not info:
                print(f"  {vid}: no cached download; skipping", file=sys.stderr)
                continue
        else:
            audio_path, info = download_episode(vid, ep_dir, proxy=args.proxy)
            if not audio_path:
                print(f"  {vid}: download failed; skipping", file=sys.stderr)
                continue
        print(f"  {vid}: {info.get('title', '?')[:60]} "
              f"({info.get('duration', 0) / 60:.0f} min)")
        blocks, suspects, words, sponsors = detect_episode(
            mod, audio_path, info, ep_dir)
        print(f"    {len(words)} words, {len(sponsors)} sponsors, "
              f"{len(blocks)} blocks: "
              + ", ".join(f"{s:.0f}-{e:.0f}" for s, e, _ in blocks))
        predictions[vid] = {
            "title": info.get("title"),
            "blocks": [[round(s, 2), round(e, 2), l] for s, e, l in blocks],
            "suspects": [[round(s, 2), round(e, 2), l] for s, e, l in suspects],
        }
        with open(os.path.join(ep_dir, "predictions.json"), "w") as f:
            json.dump(predictions[vid], f, indent=2)

    for vid in predictions:
        r = score_episode([tuple(b[:2]) for b in predictions[vid]["blocks"]],
                          gt[vid], iou_threshold=0.5)
        r["episode"] = vid
        r["segment_f1_iou03"] = score_episode(
            [tuple(b[:2]) for b in predictions[vid]["blocks"]],
            gt[vid], iou_threshold=0.3)["segment_f1"]
        episode_results.append(r)

    results = {"episodes": episode_results,
               "aggregate": aggregate(episode_results, 0.5)}
    report = format_report(results, f"BLAB ad detection — {args.label}")
    print(report)

    out_pred = os.path.join(args.work_dir, f"predictions_{args.label}.json")
    with open(out_pred, "w") as f:
        json.dump(predictions, f, indent=2)
    out_metrics = os.path.join(args.work_dir, f"metrics_{args.label}.json")
    with open(out_metrics, "w") as f:
        json.dump(results, f, indent=2)
    out_md = os.path.join(args.work_dir, f"report_{args.label}.md")
    with open(out_md, "w") as f:
        f.write(report)
    print(f"\nwrote {out_pred}\nwrote {out_metrics}\nwrote {out_md}")


if __name__ == "__main__":
    main()
