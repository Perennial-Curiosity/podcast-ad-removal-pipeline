# Testing and validating the ad detector

This directory contains everything needed to test the ad detector against
human-verified ground truth and to score changes to it.

Two free ground-truth sources are supported:

1. **BLAB (Brutally Long Audio Bench)** — Advertisement Localization task.
   200 full-length podcast episodes with human-verified ad segment
   timestamps ([paper](https://arxiv.org/abs/2505.03054),
   [data](https://huggingface.co/datasets/oreva/blab_long_audio)).
   Free download; released for **evaluation only, not training** — which is
   exactly the use case here.
2. **PodRanker ad study reports** — 128 episodes with timestamped ad blocks
   ([blog](https://podranker.com/blog/podcast-ad-study-we-timed-every-ad-in-128-episodes/)).
   The blog offers the reports as "structured JSON" but ships no public
   download link; the loader below accepts the report format if you obtain
   the files (e.g. by contacting PodRanker). Their measurements are also
   geo-targeted (ads differ by country/day), so treat them as silver
   ground truth.

## Scoring: `score_segments.py`

Scores predicted ad blocks against ground truth. Two complementary views:

- **Segment-based** precision / recall / F1 at an IoU threshold
  (default 0.5; each GT ad matched by at most one prediction). The strict
  "did you find the ad" view.
- **Time-based** precision / recall (interval overlap, equivalent to
  per-second scoring). Robust to boundary jitter; punishes over-cutting.

Standalone use (predictions = any JSON mapping episode key to segments):

```bash
python3 eval/score_segments.py --predictions my_predictions.json \
    --gt ~/blab/advertisement_localization.json
# PodRanker-style reports instead:
python3 eval/score_segments.py --podranker --gt ~/podranker_reports/ \
    --predictions my_predictions.json
```

## End-to-end run: `run_blab_eval.py`

Downloads BLAB episodes, transcribes them with the pipeline's own chunked
whisper, runs `detect_ad_blocks`, and scores the result. Audio, transcripts
and metadata are cached under `eval_work/`, so re-running detection after a
code change (`--skip-download`) only takes seconds per episode.

```bash
# 12-episode seeded sample
python3 eval/run_blab_eval.py --count 12 --label mylabel

# specific episodes
python3 eval/run_blab_eval.py --episodes iICjXqb-jmI,ACZrZ9Bm-8I --label fix

# re-run detection only (episodes already cached)
python3 eval/run_blab_eval.py --episodes iICjXqb-jmI --label fix2 --skip-download

# in an environment where YouTube blocks direct downloads
python3 eval/run_blab_eval.py --proxy http://127.0.0.1:18080 --label mylabel
```

Outputs in `eval_work/`: `predictions_<label>.json`,
`metrics_<label>.json`, `report_<label>.md`.

The BLAB annotations download automatically from Hugging Face (or pass
`--gt path/to/advertisement_localization.json`).

## What the metrics mean for this pipeline

`detect_ad_blocks` is deliberately precision-first: a false cut takes real
podcast content away from the listener, so it only auto-cuts on
high-confidence evidence and ships uncertain candidates as "suspects" for
owner review. When reading the reports:

- Segment recall is the headline number (missed ads = ads left in).
- Time-based precision below segment precision means the detector's
  boundaries are too wide (cutting content around ads).
- The suspects list is a recall lever: a suspect reel means the pipeline
  was unsure, not that it's blind.

## Results

See `results/SUMMARY.md` for the baseline vs improved numbers on the
same 12-episode BLAB sample (35 GT ads, ~35 min of ad time), with
per-error analysis. Headline: segment F1 18.5% → 40.7%, false
positives 14 → 8 (remaining ones partially cover real ads), and a
documented category of BLAB "ads" (tour-date plugs, cross-show promos,
guest social plugs) that this pipeline intentionally keeps as
content.
