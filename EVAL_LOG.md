# Local evaluation log — ad boundary work

Nothing in this log was pushed to GitHub. Times are America/Chicago (CT).

## Ground truth available (and not)

- **BLAB advertisement localization** (200 episodes, 579 ads): public
  timestamps + word-level alignments, cached at
  `eval_work/blab_gt/advertisement_localization.json`. Used for all
  numbers below unless noted.
- **Owner marks for Unsubscribe 286 (9 ads, 673s) and 287 (8 ads, 531s),
  and Security Now 1101:** not in this repo. TESTS.md says the lists
  live in the live working copy. Only the *dynamic* 286/287 times
  published in TESTS.md entry 18 are recorded in
  `eval/groundtruth/owner_partial.json` (4 ads on 286, 3 on 287). No
  audio or transcripts for those episodes are on this machine, so they
  were not scored. Do not treat those seven times as complete episode
  truth.
- SponsorBlock 25-episode set: documented in TESTS.md, files not in
  the repo.

## Harness

- Imported PR #1 (`blab-validation`) scorer and runner onto local
  branch `grok/eval`, then merged into working branch `grok/boundary-work`.
- Extended `eval/score_segments.py` with coverage matching (overlap
  ≥ 50% of the GT ad, the TESTS.md rule) and boundary error
  (mean/median |start| and |end|, share within 1s/2s/5s).
- `eval/run_word_eval.py` runs `detect_ad_blocks` on BLAB word
  timings (no Whisper, no audio). Silences are inter-word gaps ≥ 0.8s.
  Voice/reverb are off. YouTube descriptions were fetched for the
  published 12-episode sample via yt-dlp `android` client (1 of 12
  initially failed, later succeeded). origin/main's `extract_sponsors`
  finds almost no YouTube blurbs; an *eval-only* URL+CTA supplement
  (`eval/youtube_sponsors.py`, from PR #1) is applied equally to every
  pipeline under test.
- `eval/run_snap_diagnostic.py` measures `_snap_block_edges` on each
  GT ad with a paired (not rematched) sloppy block: mid-read (edges
  pulled inward by up to 8s) and overshoot (edges pushed outward by
  10s). First diagnostic (snap-diag-v1) rematched ads across episodes
  and is invalid; ignore it.

## Experiment 1 — origin/main vs grok/ad-boundary-snapping, detect+score

Setup: official 12-episode BLAB sample from `eval/results/blab_baseline.md`,
BLAB words, YouTube descriptions, no audio. 2026-11-15.

| | Segment P | Segment R | Segment F1 | Coverage R | Time P | Time R | Mean |err| | Med |err| | ≤2s | ≤5s | Matched edges |
|---|---|---|---|---|---|---|---|---|---|---|---|
| origin/main | 100% | 5.7% (2/35) | 10.8% | 5.7% | 64.8% | 14.7% | 41.81s | 11.42s | 25.0% | 50.0% | 4 |
| snapping v1 (unconditioned pause snap) | 100% | 5.7% (2/35) | 10.8% | 5.7% | 64.8% | 14.7% | 41.78s | 11.35s | 25.0% | 50.0% | 4 |

Both versions found the same two chapter-labeled ads (ACZrZ9Bm-8I,
cAgxuqGMUYg). One start moved 0.13s. Recall is low because this path
has no voice/reverb and many BLAB ads have no usable description
sponsor. This 12-episode detect run is too small to judge boundary
changes. Published PR #1 baseline (F1 18.5% with Whisper + audio) is
not comparable to this word-timestamp, no-audio setup.

## Experiment 2 — snap diagnostic v1b (unconditioned pause snap)

579 BLAB ads, paired scoring. Pipeline: unconditioned ±6s pause snap
after cue/sign-off inclusion (the original grok/ad-boundary-snapping
behavior).

| Condition | | Mean | Med | ≤1s | ≤2s | ≤5s | Worst | better/worse/same |
|---|---|---|---|---|---|---|---|---|
| mid-read (in 8s) | before | 6.04s | 8.00s | 5.4% | 15.2% | 30.9% | 8.00s | — |
| mid-read | after | 5.38s | 5.54s | 14.4% | 23.0% | 45.6% | 15.90s | 555 / 241 / 362 |
| overshoot (out 10s) | before | 9.59s | 10.00s | 2.3% | 2.6% | 3.7% | 10.00s | — |
| overshoot | after | 10.13s | 10.24s | 2.5% | 3.2% | 10.1% | 29.97s | 326 / 612 / 220 |

Verdict: helps the owner's "starts mid-read / misses the sign-off"
case (median and within-1s both improve). Hurts already-wide edges
(median worse, 612 edges worse vs 326 better). Same shape as the
rejected TESTS.md entry 4 silence+sentence snap. **Not kept as the
final snap rule.**

## Experiment 3 — snap diagnostic v2 / v2b (guarded pause snap)

Change: pause-snap only around a cue or sign-off that actually moved
the target, or to pull an edge out of the middle of a word. v2 always
moved a mid-word start to the word start (systematic early bias).
v2b moves to the nearest word edge instead.

| Condition | version | Mean | Med | ≤1s | ≤2s | ≤5s | Worst | better/worse/same |
|---|---|---|---|---|---|---|---|---|
| mid-read | v2 | 5.93s | 7.64s | 5.7% | 15.6% | 31.7% | 15.90s | 511 / 6 / 641 |
| mid-read | v2b | 6.04s | 7.86s | 5.5% | 15.4% | 31.2% | 15.90s | 191 / 178 / 789 |
| overshoot | v2 | 9.43s | 10.01s | 2.6% | 3.5% | 7.3% | 29.97s | 61 / 466 / 631 |
| overshoot | v2b | 9.32s | 10.00s | 2.6% | 3.6% | 7.3% | 29.97s | 245 / 156 / 757 |

Verdict: **keep v2b.** Overshoot mean improves (9.59→9.32) with median
held at 10.00s and more better than worse edges. Mid-read median
improves slightly (8.00→7.86); the within-1s rate is essentially
unchanged (5.4%→5.5%) because most BLAB reads have no "brought to you
by" / "thanks for supporting" cue — that is also why this is much
weaker than v1b on mid-read. Two remaining ~25–30s start errors are
false cue lookbacks that pulled an already-early start further out
(`nIYX92DDbzs`, `ySyQ7VmlrBI`).

This does not repeat the rejected unconditioned silence snap.

## Next ideas (not yet measured)

- Re-run detect+score on more than 12 BLAB episodes once more
  YouTube descriptions are cached; still no audio, so recall will
  stay below the PR #1 audio baseline.
- After Tuesday: score Unsubscribe 286/287 and SN 1101 against the
  owner's full marks, with real Whisper transcripts.
- Cue lexicon: PR #1's "good friends at" (measured, not yet applied).
- Later process_episode steps (xep, dynamic slots, split rescue) can
  still move edges after detect_ad_blocks; not exercised here (no
  episode history, no second download).
