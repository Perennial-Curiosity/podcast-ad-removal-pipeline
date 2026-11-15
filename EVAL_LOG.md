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

## Experiment 4 — truncation-aware snap (v1 / v1b / v1c / v1d)

Setup: same paired snap diagnostic as exp. 2–3, all 579 BLAB ads.
New mixed condition: each edge independently jittered inward (up to 8s),
outward (10s), or left exact, seeded with
`random.Random(f"{seed}|{vid}|{i}|{round(s,3)}|{round(e,3)}")`, seed=42.
Added in `eval/run_snap_diagnostic.py`. 2026-11-15 CST.

Policy under test (kept, v1d):

- Per edge, classify truncated vs wide. Outward moves only when truncated
  with lexical evidence: an opening cue or ad-ish language (CTA / URL /
  promo / brand / toll-free) just outside the edge, or a missed sign-off.
  Mode is "full" in that case: cue inclusion plus ±SNAP_WINDOW pause snap.
- When wide (opening cue or return-to-content phrase already inside, or
  no lexical evidence outside): only inward snaps to a cue, sign-off, or
  a pause whose dropped prefix/suffix contains a topic-shift phrase.
  Never outward.
- Outward cue / sign-off lookback capped at 8s
  (`CUE_LOOKBACK_OUTWARD`), so an already-early start cannot be pulled
  20–30s earlier (the v2b failures on `nIYX92DDbzs` and `ySyQ7VmlrBI`).
  Inward cue search still uses 20s.
- OPEN_CUES adds PR #1-style "our good friends at", "good friends at",
  "our friends at", and "this episode is sponsored".
- Mid-word uses the nearest word edge (v2b), including a slight outward
  nudge that stays on the same word. A 5s minimum-span revert no longer
  undoes a cue / sign-off / full-truncation snap on a short ad.
- `norm_words` strips a leading `#`. BLAB word timestamps prefix many
  tokens (`#brought`, `#sponsors`); without that, cues and CTAs never
  matched on this set. Trailing `.!?` is kept for `thanks_end`.

Guarded mixed baseline (v2b, re-run with the new mixed condition, same
seed; mid-read / overshoot match exp. 3):

| Condition | version | Mean | Med | ≤1s | ≤2s | ≤5s | Worst | better/worse/same |
|---|---|---|---|---|---|---|---|---|
| mid-read | before | 6.04s | 8.00s | 5.4% | 15.2% | 30.9% | 8.00s | — |
| mid-read | guarded v2b | 6.04s | 7.86s | 5.5% | 15.4% | 31.2% | 15.90s | 191 / 178 / 789 |
| mid-read | uncond. (exp. 2, reverted) | 5.38s | 5.54s | 14.4% | 23.0% | 45.6% | 15.90s | 555 / 241 / 362 |
| mid-read | trunc v1d (kept) | 5.51s | 6.53s | 9.9% | 20.8% | 41.9% | 23.40s | 376 / 234 / 548 |
| overshoot | before | 9.59s | 10.00s | 2.3% | 2.6% | 3.7% | 10.00s | — |
| overshoot | guarded v2b | 9.32s | 10.00s | 2.6% | 3.6% | 7.3% | 29.97s | 245 / 156 / 757 |
| overshoot | uncond. (exp. 2, reverted) | 10.13s | 10.24s | 2.5% | 3.2% | 10.1% | 29.97s | 326 / 612 / 220 |
| overshoot | trunc v1d (kept) | 8.60s | 10.00s | 5.2% | 8.5% | 16.1% | 15.90s | 343 / 162 / 653 |
| mixed | before | 5.20s | 7.91s | 36.3% | 39.8% | 44.6% | 10.00s | — |
| mixed | guarded v2b | 5.16s | 7.16s | 35.7% | 39.8% | 45.3% | 24.31s | 149 / 119 / 890 |
| mixed | trunc v1d (kept) | 4.86s | 4.70s | 35.7% | 41.2% | 51.4% | 22.12s | 235 / 173 / 750 |

Ablations (not kept):

- **v1 (sentence-mode outward):** mid-sentence / mid-word with no lexical
  evidence was treated as truncated and extended to the current sentence
  boundary. Mid-read ≤5s 31.2% → 44.9% (mean 5.00s, close to unconditioned
  45.6%), but overshoot mean 9.32s → 10.31s (past the 0.3s budget;
  164 better / 545 worse). Mixed mean 5.16s → 5.40s, ≤1s 35.7% → 30.7%.
  Reverted. Mid-sentence ad body and mid-sentence host talk are not
  separable without a lexical cue.
- **v1b (inward-only mid-word on wide edges):** overshoot mean 9.16s and
  552 / 12 better/worse, but mid-read mean 6.04s → 6.16s with 3 / 523
  better/worse: mid-read cuts that land mid-word and get classified as
  wide were pushed later (start) or earlier (end). Reverted to nearest
  mid-word.
- **v1c (policy above, before the `#` strip):** mid-read essentially tied
  with guarded (mean 6.04s, ≤5s 31.0%). Overshoot mean 9.25s, worst
  15.90s (the 8s cap working). Mixed mean 5.14s. Incremental, then
  superseded by v1d once cues actually matched on BLAB tokens.

Verdict: **keep v1d.** Mid-read, overshoot, and mixed means all improve
vs guarded v2b (6.04→5.51, 9.32→8.60, 5.16→4.86), none regress, and
mixed clearly improves (median 7.16→4.70, ≤5s 45.3%→51.4%). Mid-read
≤5s 31.2% → 41.9% recovers most of the unconditioned 45.6% gain without
the overshoot damage (overshoot mean is *better* than guarded, worst
29.97s → 15.90s). Three mid-read edges still jump ~20–23s inward to a
later cue inside the read (`1QMZ1Mht27k` start 507.4→522.8, GT 499.4;
`HtUYwmvaUd8` similar; `B33Xbmu1CBk` end pulled in early). Next idea:
don't snap a start inward to a cue far past the current edge when the
block already looks truncated.

A long-pause "outside only vs inside only" signal was measured as a
possible sentence-mode gate (gap 0.8–2.0s, window 6–12s) and is too
weak: even the best split (0.8s gap, 10s window) is 26% mid-read vs 13%
overshoot "outside only", which would blow the 0.3s overshoot budget.

## Experiment 5 — 12-episode detect recall, why 2/35, cheap signals

Same official 12-episode BLAB sample, word timestamps, YouTube
descriptions, no audio. 2026-11-15 CST.

Detect+score after v1d (includes the `#` strip, which also unblocks
brand / CTA matching in `detect_ad_blocks`):

| | Seg P | Seg R | Seg F1 | Time R | Matched edges |
|---|---|---|---|---|---|
| origin/main / guarded (exp. 1) | 100% | 5.7% (2/35) | 10.8% | 14.7% | 4 |
| trunc v1d | 75.0% | 8.6% (3/35) | 15.4% | 19.4% | 6 |

The extra hit is `1sOrZTP4pUQ` (MAPS Strong), a brand path that only
fires once `#` is stripped. One new FP (`xAlaCKk2UWg` Sheath Underwear):
the heatmap found the right mid-roll (624–668s) but covered only 22s of
a 79s GT ad (646–725s), so it scores as FP + miss under the 50%
coverage rule. Several other description-brand hits now show up as
single-vote SUSPECT brand-orphans (`bitbox`, `USAA`, `magicspoon`,
`seed`, `paleovalley`) and are not promoted to blocks.

Lexical signals *inside* the 35 GT ads, before vs after the `#` strip:

| Signal in the GT span | before `#` strip | after `#` strip |
|---|---|---|
| any cue / CTA / URL / promo / phone | 0/35 | 27/35 |
| opening cue | 0/35 | 12/35 |
| CTA phrase | 0/35 | 22/35 |
| promo code | 0/35 | 10/35 |
| URL-like token | 0/35 | 1/35 |
| mention of a parsed description sponsor | 7/35 (hash-blind) | 5/35 |

URL-like stays low because many spoken URLs are smashed
(`xmondohaircom`, `mphormonescom`) with no dot / "dot com" token.

Cheap standalone detectors (coverage ≥50% of GT, measured, not wired
into the pipeline):

- 15s sliding windows with any cue/CTA/URL/promo, merged: **R 34.3%
  (12/35), P 12.4% (12 TP / 85 FP)**. Too noisy to add as a method
  on its own; the signal is in the ads, but "go to" / "sponsor" also
  appear in content.
- ±15s pads around description-sponsor mentions, main
  `extract_sponsors` only: 1/12 episodes parse any sponsor, R 2.9%
  P 100%.
- Same pads with the eval-only PR #1 YouTube URL+CTA supplement: 9/12
  episodes have a parsed sponsor, **R 14.3% (5/35) P 38.5% (5 TP / 8
  FP)**. Better recall than today's detector, but precision is not
  good enough to promote every mention. The 8 FPs are mentions
  outside ads.

Main `extract_sponsors` almost never parses these YouTube blurbs
(1/12). The eval supplement is what finds most description brands.
A production port of that parser, plus a tighter vote rule for
brand-orphans that already sit on a CTA/cue, is the cheapest
plausible recall lift. Smashed-domain URL recovery is the other.

## Next ideas (not yet measured)

- Don't snap a start inward to a cue far past the current edge when
  the block already looks truncated (the 20–23s mid-read inward jumps).
- Promote single-vote brand-orphans when the span also has a CTA or
  opening cue (several 12-ep suspects already look like true ads).
- Port the PR #1 YouTube description parser onto main, then re-score
  detect+score (mentions exist; votes often don't).
- Recover smashed spoken URLs (`xmondohaircom`) as url-method anchors.
- After Tuesday: score Unsubscribe 286/287 and SN 1101 against the
  owner's full marks, with real Whisper transcripts (no `#` prefixes).
- Later process_episode steps (xep, dynamic slots, split rescue) can
  still move edges after detect_ad_blocks; not exercised here.
