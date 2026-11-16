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

## Experiment 6 — inward-snap cap, orphan promotion, show-notes
parser, smashed URLs, 48-ep eval

Local branch `grok/boundary-work` only. Nothing pushed. 2026-11-15 CST.
Baseline is truncation-aware snap v1d (commit `24a7d34`) plus the `#`
strip. Official detect scorer remains coverage overlap ≥50% of GT
duration. Snap keep-rule unchanged: mid-read and overshoot both
improve, or mixed clearly improves without either mean regressing
>0.3s vs the kept version (then v1d).

### 6a. Don't snap a truncated start inward to a far-later cue

v1d had three mid-read edges that jumped ~20–23s inward to a cue well
past the current edge (`1QMZ1Mht27k` start 507.4→522.8, GT 499.4;
`HtUYwmvaUd8` start 3869.65→3881.89; `B33Xbmu1CBk` end 967.38→951.98).

Tried and reverted before the kept cap:

- v3: hard 6s mid-like inward cap. Overshoot mean 8.60→9.11s, over the
  0.3s budget.
- v3b: only allow a large inward cue snap when a ≥0.8s pause sits
  beside the cue. Overshoot mean 9.14s, still over budget (the 10s
  overshoot diagnostic's legitimate cue trims were blocked).

Kept (v3c snap): refuse an inward cue/sign-off snap larger than
`CUE_INWARD_WHEN_UNCERTAIN = 12s` unless a long pause (≥0.8s) marks a
real opening/ending. Far interior cues no longer classify a mid-sentence
edge as "wide". Truncated starts no longer extend `cue_hi` to the first
brand mention.

Paired snap diagnostic, 579 ads, no sponsors (same harness as exp. 4).
v3c vs v1d:

| Condition | | mean | median | ≤1s / ≤2s / ≤5s | worst | better/worse/same |
|---|---|---|---|---|---|---|
| mid-read | v1d | 5.51s | 6.53s | 9.9/20.8/41.9% | 23.40s | 376/234/548 |
| mid-read | v3c | 5.46s | 6.51s | 10.0/20.9/42.0% | 22.12s | 379/232/547 |
| overshoot | v1d | 8.60s | 10.00s | 5.2/8.5/16.1% | 15.90s | 343/162/653 |
| overshoot | v3c | 8.75s | 10.00s | 4.6/7.9/14.3% | 15.90s | 327/168/663 |
| mixed | v1d | 4.86s | 4.70s | 35.7/41.2/51.4% | 22.12s | 235/173/750 |
| mixed | v3c | 4.91s | 4.86s | 35.4/40.9/50.7% | 22.12s | 231/176/751 |

Overshoot mean +0.15s, mixed +0.05s, both within 0.3s. Mid-read slightly
better; the 20–23s *start* jumps are gone. One 22.12s *end* remains on
`B33Xbmu1CBk` (a "thanks to our sponsor" 15s early still looks
ending-like). Snap change kept. `test_truncated_start_not_pulled_inward_to_late_cue`
and `test_truncated_end_not_pulled_inward_to_early_signoff` lock this in.

### 6b–6d. Detect changes, measured on 12 and 48 episodes

The official 12-episode set is too small to judge (35 GT ads, 3–5 TPs).
Expanded detect+score to 48 BLAB episodes with cached YouTube
descriptions (12 official + 36 extras; 4 of 40 fetches failed). 135 GT
ads, 8101s of labeled ad time. Same harness as exp. 5: BLAB words,
inferred silences ≥0.8s, no audio/voice/reverb.

A first detect pass (untightened "v3c") that put smashed stems into
bare `find_url_positions` and promoted uncapped brand-orphans was **not
kept**: 12-ep coverage P collapsed 75.0%→54.5% (garbage stems `inter`,
`oppo`, `para`, `what`, `usa`; a 441s `seed` orphan; the ACZr chapter
TP was lost). Tightened before the keep decision:

- Smashed spoken URLs (`xmondohaircom`, `mphormonesdotcom`) are
  recovered only in `find_url_mentions`, CTA-gated, stem length ≥6.
  They are *not* fed to the bare URL method. Short leftover stems
  (`paracom`, `intercom`, `welcome`) stay out.
- Brand-orphan promotion: a single-vote SUSPECT is marked if a
  CTA/cue/promo/URL/sign-off (not the brand mention itself) lands
  within 15s of a mention. Promoted duration is capped at 150s; a
  mention span wider than that is skipped (`seed` 441s).
- `extract_sponsors` now parses YouTube / show-notes blurbs in
  production: URL + promo/CTA in the same blank-line paragraph,
  "brought to you by" / "presented by", "Sponsors:" headers, `#ad`,
  and affiliate `?ref=` links. CTAs are attributed forward to the
  URL they introduce (`Visit Butcher Box\nURL`), not to a closer
  related-article link (pubmed, nypost, ncbi). Generic word variants
  (`news`, `plan`, `eats`) are not added. The loose PR #1 150-char
  window is intentionally *not* copied; it was claiming footer and
  article links. Eval `--no-supplement` scores this production path
  alone; on 48 episodes it matches the supplement-on run and drops
  one FP.

### Detect+score (coverage ≥50% of GT is official)

12-episode official set (35 GT ads, 2072s), supplement on (fair vs v1d):

| | Cov P | Cov R | Cov F1 | Time P | Time R | Time F1 | mean \|err\| | ≤5s | matched edges |
|---|---|---|---|---|---|---|---|---|---|
| v1d (`24a7d34`) | 75.0% (3 TP / 1 FP) | 8.6% (3/35) | 15.4% | 71.1% | 19.4% | 30.5% | 30.21s | 50.0% | 6 |
| v3e (kept) | 83.3% (5 TP / 1 FP) | 14.3% (5/35) | 24.4% | 70.5% | 24.3% | 36.1% | 20.25s | 50.0% | 10 |
| v3e production, no supplement | 83.3% (5/1) | 14.3% (5/35) | 24.4% | 70.5% | 24.3% | 36.1% | 20.25s | 50.0% | 10 |

New 12-ep TPs are promoted `bitbox` orphans on `1m1ye7pTgs0` and
`LXB0d_3WntM`. The ACZr chapter match is restored (253.0–371.1 vs GT
252.2–371.1). The one FP is still `xAlaCKk2UWg` Sheath Underwear
(620–668s covers only 22s of a 79s GT ad at 646–725s). USAA was not
promoted (no CTA in the corroboration window).

48-episode expanded set (135 GT ads, 8101s):

| | Cov P | Cov R | Cov F1 | Time P | Time R | Time F1 | mean \|err\| | ≤5s | matched edges |
|---|---|---|---|---|---|---|---|---|---|
| v1d (`24a7d34`) | 50.0% (9 TP / 9 FP) | 6.7% (9/135) | 11.8% | 74.9% | 14.7% | 24.6% | 23.19s | 44.4% | 18 |
| v3e, supplement on | 62.2% (23 TP / 14 FP) | 17.0% (23/135) | 26.7% | 74.7% | 28.4% | 41.2% | 20.67s | 41.3% | 46 |
| v3e production, no supplement | 63.9% (23 TP / 13 FP) | 17.0% (23/135) | 26.9% | 75.5% | 28.2% | 41.0% | 20.67s | 41.3% | 46 |

Coverage F1 more than doubles (11.8%→26.9%). Time recall nearly
doubles while time precision holds. Mean boundary error on matched
ads improves slightly; ≤5s dips 44.4%→41.3% because the matched set
is larger (18→46 edges), including harder mid-roll reads. Worst edge
is the same 144.34s overshoot already present in v1d (`cAgxuqGMUYg`).

### What stuck, what was reverted

Kept, as v3e (this commit):

1. Snap v3c (12s inward cap + long-pause exception).
2. CTA-corroborated brand-orphan promotion, duration capped at 150s.
3. Production `extract_sponsors` show-notes / YouTube parser (not
   eval-only). Production path equals or slightly beats the old
   eval supplement.
4. CTA-gated smashed-URL recovery with min stem length 6.
5. 48-episode eval ID list and `--no-supplement` eval flag.

Reverted or tightened before keep:

- Snap v3 and v3b (overshoot mean exceeded the 0.3s budget).
- Smash recovery inside bare `find_url_positions` (12-ep P 75%→54.5%,
  lost the ACZr chapter TP).
- Uncapped orphan promotion (`seed` 441s).
- Nearest-URL-only 150-char context (claimed pubmed / nypost /
  "News" from "today's sponsors:").
- Copying the supplement's related-link FPs (`ncbi`, `fastcompany`,
  `mindpumpfree` footer, etc.).

Unit tests: 31 passed (`tests/test_boundaries.py`,
`tests/test_sponsors_and_urls.py`, `tests/test_detect_smoke.py`,
`tests/test_score_segments.py`).

### Next ideas (not yet measured) were taken up in experiment 7.

## Experiment 7 — transcript-only recall without show-notes sponsors

Local branch `grok/boundary-work` only. Nothing pushed. 2026-11-15 CST.
Baseline is `0280e17` (v3e). Official detect scorer remains coverage
≥50% of GT. Snap keep-rule unchanged (0.3s). New eval resource: the
spliced-ad set at `/workspace/pad-dataset` (20 V4V hosts, 71 GT spans,
cached Whisper transcripts). Dataset was not modified; evals were run
read-only via `PAD_PIPELINE` snapshots. Goal: raise recall when show
notes list no sponsors, without hurting BLAB coverage F1, V4V FP, or
snap.

Diagnosis on the 0280e17 spliced baseline: 27 of 59 misses already had
a `find_url_mentions` hit, but the marked block was a 20–50s fragment
around the closing URL inside a 100–250s read, so it failed 50%
coverage and scored as FP+miss. 48 of 71 ads contain a commercial
signal (slash-path, use code, brought to you, get started, …).

### What was tried

Kept (v4b):

1. `discover_autosponsors`: spoken URL + a *commercial* corroborator
   (slash, use code, sponsored, free trial, get started, learn more,
   percent off, brought to you) becomes a `source=auto` sponsor and is
   fed into brand / brand-orphan / cluster. The mention-span cap still
   applies only to description-listed sponsors, so auto brands do not
   shrink a long read down to the closing URL.
2. CTA fallback `_expand_url_tail`: if there is no opening cue, look
   back ~75s from the closing URL. Gated on the same commercial
   corroborator and vetoed when V4V donation vocabulary sits nearby.
3. `_merge_lexical_pods`: join nearby cta/url/brand blocks with a gap
   ≤90s (multi-sponsor mid-roll pods). Voice/reverb/duplicate-only
   blocks are not merged.
4. Auto-sponsor skip list plus substring veto (`meetup`, `donation`,
   `donate`, `boostagram`) and a nearby V4V-vocab veto, so "go to
   donations.com" / meetup URLs are not promoted.

Reverted (v4a, unfiltered URL-tail expand): applying `_expand_url_tail`
to every CTA+URL, including plain "go to X.com", raised spliced F1
20.2%→23.4% and BLAB coverage F1 26.9%→40.2%, but V4V donation-segment
FP time share went 2.7%→4.9% (`donations`, `meetups`, `podcastindex`,
`fountain`). Clean-content FP was unchanged; the gate still failed.
Tightened to v4b before keep.

Not measured in this batch (deferred): audio voice/room-change
detectors (splices make this artificially easy; BLAB should lead),
and cross-episode duplicate matching of repeated ads.

### Results

Snap diagnostic (579 ads): identical to v3c / `0280e17`. Mid-read
after 5.46s, overshoot 8.75s, mixed 4.91s. No regression.

Spliced set, show-notes-only (71 GT ads, 20 episodes):

| | Seg/Cov P | Seg/Cov R | Seg/Cov F1 | Time P | Time R | Time F1 | mean \|err\| | ≤5s |
|---|---|---|---|---|---|---|---|---|
| `0280e17` | 25.0% (12 TP / 36 FP) | 16.9% (12/71) | 20.2% | 95.0% | 24.3% | 38.7% | 10.87s | 54.2% |
| v4a (reverted) | 26.3% (15/42) | 21.1% (15/71) | 23.4% | 90.4% | 29.2% | 44.1% | 13.15s | 46.7% |
| v4b (kept) | 26.0% (13/37) | 18.3% (13/71) | 21.5% | 94.1% | 26.7% | 41.6% | 11.20s | 50.0% |

Oracle / show-notes-listed condition was not re-run; the goal was
recall *without* listed brands.

BLAB 48-ep production path (`--no-supplement`):

| | Cov P | Cov R | Cov F1 | Time P | Time R | Time F1 | mean \|err\| | ≤5s |
|---|---|---|---|---|---|---|---|---|
| `0280e17` | 63.9% (23/13) | 17.0% (23/135) | 26.9% | 75.5% | 28.2% | 41.0% | 20.67s | 41.3% |
| v4a (reverted) | 62.5% (40/24) | 29.6% (40/135) | 40.2% | 61.2% | 33.8% | 43.5% | 19.77s | 35.0% |
| v4b (kept) | 58.1% (25/18) | 18.5% (25/135) | 28.1% | 77.3% | 27.7% | 40.8% | 14.86s | 48.0% |

V4V hard negatives (16 uncut hosts):

| | CTA-segment FP time | Clean FP time | Clean blocks / hour |
|---|---|---|---|
| `0280e17` | 2.7% (513s / 19034s) | 1.8% (1177s) | 1.00 (18 blocks) |
| v4a (reverted) | 4.9% (943s) | 1.8% (1177s) | 1.00 (18) |
| v4b (kept) | 3.1% (597s) | 1.8% (1177s) | 1.00 (18) |

v4b clean-content FP is unchanged. The +0.4pp donation-segment share
is mostly a real "use code" coffee ad that sits inside a No Agenda
donation cut, plus one extra CTA-touch. Treated as not a meaningful
clean-content regression.

Gates: spliced F1 up, BLAB coverage F1 up, V4V clean FP unchanged,
snap unchanged. Kept v4b.

Known leftover misses: long multi-brand pods whose URL fragments are
>90s apart still fail 50% coverage (`synth-000` 123s gap). Two
previously-good spliced TPs shrink when an auto-sponsor label pulls
refinement toward the closing mention (`granola`, a notion+deepgram
pod). Worth a follow-up: merge-then-don't-refine-to-one-brand, or a
slightly wider lexical-pod gap.

Unit tests: 37 passed.

### Next ideas (not yet measured)

- Wider or post-refinement lexical-pod merge so 120–150s gaps in
  multi-brand mid-rolls count as one ad, without shrinking to the
  first brand's mention span.
- Do not let auto-sponsor refinement shrink a wider cta/reverb block
  that already covered the ad (`granola` 2573–2694 → 2659–2694).
- Audio voice/room-change at insertion boundaries, scored primarily
  on BLAB (splices make this too easy).
- Cross-episode duplicate audio/text for ads that repeat across shows.
- After Tuesday: score Unsubscribe 286/287 and SN 1101 against the
  owner's full marks, with real Whisper transcripts (no `#` prefixes).

## Experiment 8 — grow confirmed URL/CTA fragments to the full read (v5a, kept)

Goal: the biggest leftover miss after v4b is not "no signal" —
27 of the no-notes spliced misses were already marked as a 20–50s
fragment around the closing URL inside a 100–250s read, and failed
the 50% coverage rule. Also fix two v4b regressions: description-span
capping / start-refinement shrinking good TPs toward the closing
mention (`granola` 2573–2694 → 2659–2694; a notion+deepgram pod),
and lexical pods whose fragments sit >90s apart
(`synth-000` hostinger 3238–3257 and buildkite 3380–3421, 123s gap).

Baseline: `6e34fe0` (v4b). Branch `grok/boundary-work`. Local only;
no GitHub writes.

### What landed (v5a)

1. Mention-span cap now loops `listed_sponsors` only. Auto-sponsors
   were included in `sponsors`, so a late `granulah` hit pulled the
   start in by 30s and dropped the ad under 50% coverage. The earlier
   v4b patch fixed this check in the sponsor-anchor loop, not in the
   cap itself.
2. Inward *start* refinement is skipped unless the brand is
   description-listed. Auto / unlisted brands live at the closing
   URL, so refining the start to the latest method-segment before
   that mention was the other shrink path (notion+deepgram). End
   refinement is unchanged.
3. `_grow_confirmed_fragments`, after snap, walks sentence-by-sentence
   backward and forward from a confirmed cta/url/brand/chapter/cluster
   block while the text stays ad-like, capped at 240s per edge.
   Ad-like = brand (len≥4), an opening cue (`OPEN_CUES` plus "what's
   up", "I'm here", "ceo of", "founder of", "this episode"), or
   commercial / `ADISH` language; otherwise a second-person "you/your"
   rate ≥0.08 and bag-of-words cosine to the fragment ≥ cosine to the
   episode background (tokens >60s from any current block) + 0.08,
   or fragment cosine ≥0.15. Stops at a topic-shift / `RETURN_CUES`,
   nearby V4V vocabulary, two consecutive non-ad-like sentences, or
   an opening cue (included). Growth is kept only when a stop signal
   was seen — hitting the cap with no opening or shift is rejected.
   Skip-list / meetup / donation / boostagram stems are not grown.
   Voice/reverb/duplicate-only blocks are left alone.
4. `_merge_lexical_pods` still joins gaps ≤90s. Gaps of 90–180s join
   only when the in-between text is commercial, brand, or opening,
   or ≥50% of gap sentences are ad-like, and is not V4V-ish / a
   topic shift.

Embedding model: not used. The pipeline venv has no numpy / sklearn /
sentence-transformers, and no MiniLM (or similar) weights are cached.
A post-process simulation of the same lexical + bag-of-words cosine
rules scored 20 spliced episodes in 0.3s on CPU. That is fast enough
and stays Android-portable, so no new dependency was added.

### Results

Snap diagnostic (579 ads): identical to v3c / v4b. Mid-read after
5.46s, overshoot 8.75s, mixed 4.91s. Growth runs after snap, so the
diagnostic (which scores `_snap_block_edges` only) is unchanged.

Spliced set, show-notes-only (71 GT ads, 20 episodes):

| | Cov P | Cov R | Cov F1 | Time P | Time R | Time F1 | mean \|err\| | ≤5s |
|---|---|---|---|---|---|---|---|---|
| v4b (`6e34fe0`) | 26.0% (13 TP / 37 FP) | 18.3% (13/71) | 21.5% | 94.1% | 26.7% | 41.6% | 11.20s | 50.0% |
| v5a (kept) | 55.3% (26 TP / 21 FP) | 36.6% (26/71) | 44.1% | 85.4% | 38.2% | 52.8% | 20.94s | 36.5% |

Targeted fixes held: `granola` is 2573–2694 again (was 2659–2694).
The `synth-000` hostinger+buildkite pod is one block, 3170–3421,
against GT 3173–3427. The notion pod grew 1081–1164 → 1041–1175 and
the following perplexity+coder fragments merged to 2196–2389.

Segment (IoU 0.5) is 25 TP / 22 FP, F1 42.4% — coverage and IoU now
differ slightly because a few grown blocks clear 50% of GT time but
not IoU 0.5. Official scorer remains coverage.

Mean |err| and ≤5s look worse because the new TPs are long reads whose
starts were still a bit late or, in one case, over-grown
(`synth-006` end +210s, two adjacent mid-rolls glued). Time precision
94%→85% is the same over-growth. Coverage F1 more than doubled and
precision rose, so the gate passes; tightening the cap / requiring
an opening cue for growth past ~60s is the follow-up.

BLAB 48-ep production path (`--no-supplement`):

| | Cov P | Cov R | Cov F1 | Time P | Time R | Time F1 | mean \|err\| | ≤5s |
|---|---|---|---|---|---|---|---|---|
| v4b (`6e34fe0`) | 58.1% (25/18) | 18.5% (25/135) | 28.1% | 77.3% | 27.7% | 40.8% | 14.86s | 48.0% |
| v5a (kept) | 79.1% (34/9) | 25.2% (34/135) | 38.2% | 70.0% | 36.3% | 47.8% | 23.04s | 41.2% |

Coverage F1 28.1%→38.2% (25→34 TPs), precision up, false-positive
blocks down 18→9. Signed mean start is −17.5s (was −10.8s): some
BLAB starts walk too far back (worst 132s on `u7ecuuwssic`). Same
over-growth tradeoff as the spliced set; coverage F1 is the keep
gate and it passed.

V4V hard negatives (16 uncut hosts):

| | CTA-segment FP time | Clean FP time | Clean blocks / hour |
|---|---|---|---|
| v4b (`6e34fe0`) | 3.1% (597s / 19034s) | 1.8% (1177s) | 1.00 (18 blocks) |
| v5a (kept) | 3.7% (712s) | 1.9% (1228s) | 1.00 (18) |

No new clean or CTA blocks. The +0.6pp donation-segment share and
+51s of clean time are existing FPs growing a little: No Agenda
1911 `code-itm20` 33s→84s, `noahgenderrings` +15s, `mortalspodcast`
+19s, `bunny` +18s, and a garbage `reverb/url:-words` stem on
Moe Factz (+35s clean). `meetups` did not grow (substring veto).
Treated as not a meaningful FP-rate regression — same block counts,
unlike reverted v4a (CTA 2.7%→4.9%). A follow-up can refuse to grow
generic / `code-*` / `-words` stems.

Reverted this batch: nothing. v5a passed every gate against v4b
(spliced coverage F1 up, BLAB coverage F1 up, V4V clean FP not up
meaningfully, snap unchanged).

Audio voice/room-change cues were not started; growth used the
batch's time. Judge those primarily on BLAB if they are next.

Unit tests: 41 passed, including growth to "what's up nerds / ceo
of fly", ad-like 120s pod merge, auto-sponsor blocks not capped to
the closing URL, and a donations.com fragment that must not grow.

### Next ideas (not yet measured)

- Tighten growth that overshoots: require an opening cue (not just
  two non-ad-like sentences) before accepting >60s of backward
  walk; refuse generic stems (`-words`, `code-*`) so V4V donation
  CTAs and BLAB starts do not walk as far.
- Split or refuse a merge when two GT-adjacent mid-rolls get glued
  into a 300s block (`synth-006` end +210s).
- Audio voice/room-change at insertion boundaries, scored primarily
  on BLAB (splices make this too easy).
- Cross-episode duplicate audio/text for ads that repeat across shows.
- After Tuesday: score Unsubscribe 286/287 and SN 1101 against the
  owner's full marks, with real Whisper transcripts (no `#` prefixes).

## Experiment 9 — cap over-growth and unglue adjacent mid-rolls (v5c, kept)

Goal: win back edge accuracy and time precision lost to v5a over-growth
without giving back the recall gains. Baseline: `69ff162` (v5a). Branch
`grok/boundary-work`. Local only; no GitHub writes.

Target: coverage F1 within ~2pp of v5a on spliced and BLAB, while
pulling mean |err| and signed start bias toward v4b.

### What landed (v5c)

1. Backward/forward growth past 60s is kept only when the walk hit a
   strong stop: an opening cue (back), a sign-off (forward), or a
   topic-shift / return cue. Two non-ad-like sentences or a mid-read
   brand mention is not enough to walk 2–4 minutes. Forward walks now
   stop at a sign-off (included) or at a new opening after leaving
   the fragment (that opening belongs to the next mid-roll).
2. When an opening cue sits inside a long unpunctuated ASR sentence,
   growth starts at the cue, not the sentence start — otherwise a
   "brought to you by" 90s into a run-on sentence pulls the start
   back through show talk.
3. Generic / garbage stems are not grown: labels starting with `-`
   (`-words`), `code-*` promo-code labels (`code-itm20`), stems in
   `_GENERIC_STEMS` (`words`, `episode`, `slash`, …), and the existing
   skip-list / meetup / donation veto.
4. Lexical pods still merge at ≤90s (and up to 180s when the gap is
   ad-like), but a gap with a topic-shift / return cue is not merged.
   Longer gaps are judged on the *inner* text after stripping a
   leading sign-off and a trailing opening, so the next ad's
   "brought to you by" cannot make 157s of show talk look like a pod.
   Bare "this episode" was removed from opening extras (it matched
   "when you brought up this episode to me" in regular talk);
   "this episode is also" is kept alongside `OPEN_CUES`.
5. `_split_glued_midrolls` splits a long block that contains a
   return-to-show cue, or a sign-off then a later opening with a
   non-sales island between them. Immediate pod handoffs and
   interview-style second reads stay one block.
6. Grown / split edges are re-snapped with the same v1d
   truncation-aware snap.

Audio voice/room-change cues were not started; the merge-rule
iteration used the remaining time.

### Results

Snap diagnostic (579 ads): unchanged from v3c / v4b / v5a (snap
function not edited). Mid-read after 5.46s, overshoot 8.75s, mixed
4.91s.

Spliced set, show-notes-only (71 GT ads, 20 episodes):

| | Cov P | Cov R | Cov F1 | Time P | Time R | Time F1 | mean \|err\| | ≤5s | signed start |
|---|---|---|---|---|---|---|---|---|---|
| v4b (`6e34fe0`) | 26.0% (13/37) | 18.3% (13/71) | 21.5% | 94.1% | 26.7% | 41.6% | 11.20s | 50.0% | +5.3s |
| v5a (`69ff162`) | 55.3% (26/21) | 36.6% (26/71) | 44.1% | 85.4% | 38.2% | 52.8% | 20.94s | 36.5% | +5.2s |
| v5c (kept) | 52.0% (26/24) | 36.6% (26/71) | 43.0% | 88.2% | 36.7% | 51.9% | 15.58s | 38.5% | +9.4s |

Same 26 TPs as v5a. Coverage F1 44.1%→43.0% (−1.1pp, inside the
~2pp budget). Time precision 85.4%→88.2% and mean |err| 20.94s→15.58s
move toward v4b. Worst edge 209s→74s: the `synth-006` SerpAPI mid-roll
and Perplexity post-roll are no longer glued into one 310s block
(2200–2510 → 2200–2299 + 2468–2510). Hostinger+Buildkite, Notion, and
granola TPs are kept. Signed start is a bit later (+9.4s) because
walks without an opening cue stop at 60s.

BLAB 48-ep production path (`--no-supplement`):

| | Cov P | Cov R | Cov F1 | Time P | Time R | Time F1 | mean \|err\| | ≤5s | signed start |
|---|---|---|---|---|---|---|---|---|---|
| v4b (`6e34fe0`) | 58.1% (25/18) | 18.5% (25/135) | 28.1% | 77.3% | 27.7% | 40.8% | 14.86s | 48.0% | −10.8s |
| v5a (`69ff162`) | 79.1% (34/9) | 25.2% (34/135) | 38.2% | 70.0% | 36.3% | 47.8% | 23.04s | 41.2% | −17.5s |
| v5c (kept) | 69.6% (32/14) | 23.7% (32/135) | 35.4% | 71.0% | 32.1% | 44.2% | 18.57s | 40.6% | −0.7s |

Coverage F1 38.2%→35.4% (−2.8pp), a hair over the ~2pp target, from
two lost TPs: `KWQMS_s4lu8` 2026–2218 was a barely-50% v5a match
(end −84s) that the 60s cap shrank under the line; `bdJQGeuGcpE`
1906–2076 was a clean v5a TP that shrank to 1894–1970. In return,
the `u7ecuuwssic` start −132s overshoot is gone (−1.1s), signed
mean start is −0.7s (better even than v4b's −10.8s), and mean |err|
23.04s→18.57s. Kept: the boundary win is the point of this batch,
spliced recall is intact, and BLAB is still well above v4b (35.4%
vs 28.1%).

V4V hard negatives (16 uncut hosts):

| | CTA-segment FP time | Clean FP time | Clean blocks / hour |
|---|---|---|---|
| v4b (`6e34fe0`) | 3.1% (597s / 19034s) | 1.8% (1177s) | 1.00 (18 blocks) |
| v5a (`69ff162`) | 3.7% (712s) | 1.9% (1228s) | 1.00 (18) |
| v5c (kept) | 3.7% (705s) | 1.8% (1189s) | 1.00 (18) |

Same 18 clean blocks and 13 CTA-touching blocks. Slightly cleaner
than v5a (`code-*` / `-words` no longer grow). Not a meaningful
regression vs v4b (same block counts).

Reverted this batch: nothing. An intermediate v5b that also split
true multi-brand pods (Hostinger+Buildkite, Perplexity+Coder, Base44
+KubeCon) was tightened before keep — interview-style second reads
are not a return to the show. v5c passed spliced F1, V4V, and snap
gates; BLAB F1 is 0.8pp past the soft 2pp line in exchange for the
start-bias fix.

Unit tests: 45 passed, including the 60s cap, opening cue beyond 60s,
generic-stem veto, and a "welcome back" mid-roll split that still
refuses merge.

### Next ideas (not yet measured)

- Recover the two BLAB TPs that the 60s cap shrank under 50%
  (`bdJQGeuGcpE` especially) without bringing back the −132s start.
  A mid-length cap (~90s) or "keep walking while the brand repeats"
  might be enough.
- `synth-015` Base44+KubeCon is still two fragments in the final
  pred (539–699 + 784–817); a post-snap merge pass could restore a
  more precise pod edge.
- Audio voice/room-change at insertion boundaries, scored primarily
  on BLAB (splices make this too easy).
- Cross-episode duplicate audio/text for ads that repeat across shows.
- After Tuesday: score Unsubscribe 286/287 and SN 1101 against the
  owner's full marks, with real Whisper transcripts (no `#` prefixes).


## Experiment 10 — recover the two lost BLAB TPs without the −132s start (v5f, kept)

Goal: get back `KWQMS_s4lu8` (BetUS, 2026–2218) and `bdJQGeuGcpE`
(Bitbox+BitcoinWay+CoinVigilante, 1906–2076), which v5c shrank under
50% coverage, without re-gluing `u7ecuuwssic` into a −132s start.
Also merge `synth-015` Base44+KubeCon fragments left split after snap.
Then survey a genre-diverse real-audio chapter-GT set (not tech-only)
for later voice/room-change cues.

Branch `grok/boundary-work` still on top of `a745b09` (v5c). Nothing
pushed.

### Diagnosis

The two lost TPs were not caused by the 60s growth cap itself.

- `bdJQGeuGcpE`: the heatmap already covered 1894–2076. Inward *end*
  refine then cut 2076→1970 because auto-sponsor `code-robin` is
  mentioned mid-pod, so coverage fell to 38% (FN). v5c also refused
  to grow `code-*`, so the end stayed short.
- `KWQMS_s4lu8`: heatmap 2032–2097 of a 192s BetUS read. v5a grew
  forward +37s to 2134 (53%, just over 50%). v5c treated `code-youtube`
  as a hard growth veto, so the walk never started (37s was under the
  60s cap anyway).

`u7ecuuwssic` −132s was backward growth of `freshly` into prior
content, then a v5c *split* at the Freshly opening (4s gap). Post-snap
merge must not re-glue an opening-only / empty-inner gap.

`synth-015` Base44+KubeCon: merge ran *before* the second snap; snap
pulled a grown end back to a mid-pod sign-off (~699), leaving cubcon
783–817 unmerged, and 539–699 was 48% of 583–827 (FN in v5c).

### Intermediate versions (measured, not kept)

**v5d** (blunt 30s end-refine cap + `code-*` soft-block + post-snap
merge + keep capped walks without a strong stop):

- Recovered both BLAB TPs (34/135, F1 37.4%) and kept the `u7ecuuwssic`
  split (Freshly start +0.0, not −132s).
- Spliced F1 44.3% (27 TPs), but edges got worse: mean 15.58s→23.18s,
  ≤5s 38.5%→27.8%, Time P 88.2%→81.6%. Granola ends jumped to +39s
  and +99s because a "thank you for *sponsoring*" sign-off plus a
  40s orphan-extend pad made `(e - ev_end) > 30`, so refine skipped.
- Failed the "don't make start and end times worse" rule. Not kept.

**v5e** (skip inward refine on a long tail only when the tail is still
ad-like): recovered SerpAPI's +54s end and a 35th BLAB TP
(`RuTIfLaarp4`, two FPs merged into one TP), but granola stayed long
because the sign-off sentence itself contains the ADISH word
"sponsoring". Not kept.

### v5f (kept)

- `code-*` is a soft growth block: the stem may grow with the 60s cap
  but is not used as a brand. Hard-block remains `-words` / skip-list /
  donation stems. Capped walks are kept when the walk is accepted
  (exhaustion is a soft stop, not a discard) — needed for the +37s
  BetUS walk.
- End refine no longer uses a raw 30s cutoff. A long tail is kept only
  when the text *after the last mention's sentence* is still ad-like
  (another brand, CTA, or sales talk in a multi-brand pod). The
  sign-off sentence is excluded so "thank you to granola for
  sponsoring the show" cannot block a trim. Fallback:
  `_last_adlike_end` when no method-segment end exists.
- The same end refine runs again after grow + split + post-snap merge,
  then a final snap, so a walk that overshoots after the early refine
  still gets trimmed.
- Merge: gaps >2s require `_gap_is_adlike`; an empty inner after
  stripping a sign-off / opening requires *both* a sign-off and an
  opening (opening alone is the next ad). Post-snap merge is on, so
  `u7ecuuwssic`'s 4s opening-only gap stays split.
- Early inward end refine is skipped for mid-pod promo codes
  (`bdJQGeuGcpE` code-robin at 1970s in an 1894–2076 read).

Official production path (`--no-supplement` on BLAB 48; spliced
show-notes-only; V4V uncut hosts). Snap diagnostic code path
unchanged (v1d + 12s inward cap); not re-run for v5f. After means
from v5d/v5a: mid 5.46s, overshoot 8.75s, mixed 4.91s.

Spliced show-notes-only (20 episodes, 71 ads):

| | Cov P | Cov R | Cov F1 | TP | Time P | Time R | mean \|err\| | ≤5s | signed start | worst |
|---|---|---|---|---|---|---|---|---|---|---|
| v4b (`6e34fe0`) | 26.0% | 18.3% | 21.5% | 13 | 94.1% | 26.7% | 11.20s | 50.0% | +5.3s | 75s |
| v5a (`69ff162`) | 55.3% | 36.6% | 44.1% | 26 | 85.4% | 38.2% | 20.94s | 36.5% | +5.2s | 210s |
| v5c (`a745b09`) | 52.0% | 36.6% | 43.0% | 26 | 88.2% | 36.7% | 15.58s | 38.5% | +9.4s | 74s |
| v5d (not kept) | 52.9% | 38.0% | 44.3% | 27 | 81.6% | 38.8% | 23.18s | 27.8% | +5.6s | 99s |
| v5e (not kept) | 52.9% | 38.0% | 44.3% | 27 | 82.8% | 38.6% | 22.18s | 29.6% | +5.6s | 99s |
| v5f (kept) | 52.9% | 38.0% | 44.3% | 27 | 86.8% | 38.1% | 18.94s | 33.3% | +5.6s | 84s |

F1 is 0.2pp above v5a and 1.3pp above v5c, with 27 TPs (v5c's 26
kept). Time P recovered most of the v5d loss (81.6%→86.8%, vs v5c
88.2%). Granola ends are back: synth-015 +39s→−0.5s, synth-007
+99s→−0.3s; SerpAPI end −1.7s; Perplexity +28s→−5.8s. Remaining
spliced edge gap vs v5c is mostly compositional: the new synth-015
Base44+KubeCon TP (65% coverage, end −83.9s, leftover cubcon 784–817
still unmerged) is now in the edge average, and synth-000's
Hostinger+Buildkite pod split (v5c had one 97% TP; v5f has
code-practical 3169–3297 + buildkite 3340–3421, both under 50% of
the combined GT). Post-snap merge still will not join a 41s
mid-KubeCon conference-talk gap.

BLAB 48-ep production (`--no-supplement`):

| | Cov P | Cov R | Cov F1 | TP/135 | Time P | Time R | mean \|err\| | ≤5s | signed start | worst |
|---|---|---|---|---|---|---|---|---|---|---|
| v4b | 58.1% | 18.5% | 28.1% | 25 | 77.3% | 27.7% | 14.86s | 48.0% | −10.8s | 109s |
| v5a | 79.1% | 25.2% | 38.2% | 34 | 70.0% | 36.3% | 23.04s | 41.2% | −17.5s | 132s |
| v5c | 69.6% | 23.7% | 35.4% | 32 | 71.0% | 32.1% | 18.57s | 40.6% | −0.7s | 101s |
| v5d (not kept) | 72.3% | 25.2% | 37.4% | 34 | 72.2% | 37.9% | 17.42s | 41.2% | −5.5s | 84s |
| v5e (not kept) | 76.1% | 25.9% | 38.7% | 35 | 72.5% | 37.7% | 17.67s | 41.4% | −3.5s | 84s |
| v5f (kept) | 76.1% | 25.9% | 38.7% | 35 | 75.2% | 37.6% | 16.55s | 45.7% | −3.5s | 84s |

Both lost TPs recovered: `KWQMS_s4lu8` 2033–2134 (53%, start +6.4,
end −84.1, same as v5a); `bdJQGeuGcpE` 1894–2076 (100%, start −12.7,
end +0.3). A third TP appeared on `RuTIfLaarp4` (v5c/v5d had two FPs;
v5f merges them to a 4116–4217 vs GT 4052–4212 TP). `u7ecuuwssic`
Freshly start is +0.0 (not −132s); the following Squarespace chapter
in the same 5402–5609 GT is 5402–5611 (end +1.9, 100% coverage). The
leftover FP 5276–5397 remains. First Squarespace ad is back to v5c
accuracy (5.4–35.0, end +3.3; v5d/v5e had +17.9). Worst remaining
edge is still the short BetUS end (−84s). Coverage F1 is 0.5pp above
v5a and 3.3pp above v5c; ≤5s 45.7% is the best of the v5 line.

V4V hard negatives (16 uncut hosts):

| | CTA-segment FP time | Clean FP time | Clean blocks / hour |
|---|---|---|---|
| v4b (`6e34fe0`) | 3.1% (597s / 19034s) | 1.8% (1177s) | 1.00 (18 blocks) |
| v5a (`69ff162`) | 3.7% (712s) | 1.9% (1228s) | 1.00 (18) |
| v5c (`a745b09`) | 3.7% (705s) | 1.8% (1189s) | 1.00 (18) |
| v5d / v5e (not kept) | 3.7% (712s) | 1.8% (1189s) | 1.00 (18) |
| v5f (kept) | 3.7% (699s) | 1.8% (1189s) | 1.00 (18) |

Same 18 clean blocks and 13 CTA-touching blocks. CTA time is 5s under
v5c (bunny end trimmed slightly). Not a regression.

Reverted this batch: v5d (blunt 30s cap) and v5e (adlike-tail check
that still treated "sponsoring" in the sign-off as mid-pod copy).
Their TP-recovery pieces (`code-*` soft-block, mid-pod skip, post-snap
merge, keep capped walks) are kept inside v5f with the sign-off-aware
refine.

Unit tests: 50 passed, including generic `-words` still blocked,
`code-youtube` growth ≤60s, mid-pod `code robin` must not cut
CoinVigilante, long show-talk tails refine inward, and "thank you
for sponsoring" does not block a trim.

### Real-audio chapter-GT set (multi-genre, not tech-only)

The app has to work on any podcast, so this eval cannot be
Changelog/ATP alone, and audio voice/room-change cues must not be
tuned on spliced ads (those overstate insertion artifacts) or on a
tech-only slice.

Built this turn (local, under `eval_work/real_audio_gt/`, not
committed; pad-dataset inputs untouched):

- `unspliced_chapter_gt_manifest.json`: 28 already-downloaded original
  unspliced mp3s (Changelog 8, Practical AI 6, JS Party 6, ATP 8) with
  68 publisher-chapter GT ads (50 `podcast:chapters` JSON, 18 ID3
  CHAP). ATP is host-read; Changelog-network is mixed/unknown. All
  English technology. Full-episode Whisper transcripts are not
  present; transcribing ~28 hour-long shows on CPU is deferred. Kept
  as *one genre slice*, not the full set. Do not tune on this slice
  alone.
- `chapter_feed_probe.json` + `catalog.json`: probed ~100 public RSS
  feeds across true crime, comedy, news, sports, business, health,
  history, kids/family, fiction, interviews, technology, and
  non-English (de/es/fr/pt/sv). Finding: strict `Sponsor:` / `[Ad]`
  chapter titles in this pass appeared only on the Changelog network.
  Major DAI shows (Crime Junkie, SmartLess, The Daily, Huberman, The
  Bill Simmons Podcast, etc.) publish no chapter marks at all. Acquired
  had a topical "Advertising" chapter (false hit). Lage der Nation
  (German news) had a mid-show "advertise with us" callout, not a
  sponsor read. Stuff You Missed in History Class has some chapter
  payload but no ad titles in the sample. RSS-only probing undercounts
  ID3 CHAP shows (ATP is confirmed on disk; Relay FM likely similar
  and not yet downloaded). Several kids/fiction/Radiotopia feeds
  failed on SSL/404 and need alternate official URLs.
- BLAB 48 (already the official word-eval) is more genre-diverse than
  the on-disk chapter slice: comedy 11, sports 13, health/fitness 9,
  business/crypto 11, interviews 3, news-talk 1, across 12 channels.
  Missing: true crime, kids, fiction, history, non-English. Its audio
  is YouTube-blocked on this box, so it cannot judge voice/room-change
  cues.
- Other labeled sources: owner 286/287 dynamic-only marks in-repo
  (full 286/287/1101 still expected Tuesday); Hamburg 2025 MA thesis
  "Identifying Advertisements in Podcasts" (manual RSS + SponsorBlock
  YouTube; public release not confirmed); SponsorBlock dumps (same
  YouTube block here); EACL 2021 extraneous-content set used the
  withdrawn Spotify 100k corpus.

Audio voice/room-change cue work was **not started**. Spliced would
overstate it; BLAB audio is blocked here; the only ready real audio
with chapter GT is a technology slice, and tuning on that would
overfit tech-sponsor vocabulary.

### Next ideas (not yet measured)

- Join leftover mid-pod fragments: `synth-015` cubcon 784–817 still
  unmerged (41s of conference talk in the gap); synth-000
  Hostinger+Buildkite split into two sub-50% pieces (voice/reverb
  finds Buildkite, merge does not join a ~42s gap).
- The short BetUS end on `KWQMS_s4lu8` (−84s) is still the worst
  official BLAB edge; growing `code-youtube` further without a new
  overshoot would help.
- Leftover FP 5276–5397 on `u7ecuuwssic` (pre-Freshly split-off).
- Expand the real-audio GT set for genre diversity, not volume:
  retry failed kids/fiction/Radiotopia RSS URLs; ID3-CHAP probe on
  one short episode each from Relay FM and a German Podlove show;
  Podcast Index search for mid=chapters with Sponsor/Ad/Werbung
  titles; transcribe 1 short episode per genre before any of the
  28 tech hours. Include both host-read and produced/DAI ads.
  Report per-genre scores once more than one genre has
  audio+GT+transcripts.
- Only then try voice/room-change boundary cues, judged on that
  diverse real-audio set (and BLAB if audio becomes available),
  never primarily on splices.
- After Tuesday: score Unsubscribe 286/287 and SN 1101 against the
  owner's full marks, with real Whisper transcripts (no `#` prefixes).

## Experiment 11 — pre/post-roll, underwriting, start bias, batch2 gate (v5m)

Date: 2026-11-15 CST. Local only, no GitHub writes.

Targeted the weaknesses the DAI set exposed on v5f: missed pre-rolls
and post-rolls, early start bias (signed start −23.9s), and NPR-style
underwriting. Added the DAI set as a 5th official gate (per-genre F1
must not drop, overall DAI F1 and start bias must improve; DAI
precision is pessimistic because same-fill ads are unlabeled, so
judge mainly on recall and boundaries). Added genre-diverse spliced
batch2 as a 6th gate (show-notes-only F1 up; brands-listed start
bias toward 0).

Existing gates still apply: spliced batch1 show-notes-only F1 up,
BLAB 48 coverage F1 within ~2pp, V4V clean FPs not rising, snap
diagnostic within 0.3s.

### What changed (on top of v5f `c153ebc`)

Detector work in `pipeline/run_pipeline.py` (unit tests in
`tests/test_sponsors_and_urls.py` and `tests/test_boundaries.py`):

- New `_method_underwrite` for NPR-style openings ("support for this
  podcast comes from…", "this message comes from…"), with
  `_span_is_underwrite_body`, a larger edge reject budget, and
  `_underwrite_lookahead_ok` so founder-story sentences don't stop
  the walk when commercial language resumes shortly after.
- New `_method_edge_adlike` for produced language in the first/last
  90s, with edge promotion of underwrite/url/edge votes, a 15s floor
  only when those methods voted, and heatmap islands extended to the
  full overlapping edge/underwrite candidate.
- Host-boundary phrases (credits, "tune in next", "thanks for
  listening", "hey Lulu here") stop backward growth and edge
  clusters. Leading post-roll extension only walks wordless gaps,
  not host talk (Athletic early-start).
- `_span_is_show_contact` vetoes tip-line / show-inbox URLs (Crime
  Junkie FP). `_span_has_open(..., extras=False)` so "what's up" /
  "I'm here" growth extras cannot seed a V4V theme-song preroll.
- `_span_has_shift` uses word boundaries so "ok back" does not match
  "look back" (Granola mid-roll split). `_span_has_commercial` treats
  spoken TLDs (including `.io`) only after "dot", not bare "net"/"com".
- URL pause-clamp only fires when a previous other-brand group ended
  nearby (mid-read stutters before "fly .io" no longer eat the
  lookback).
- Generic single-token oracle stems ("Made", "John") are dropped
  unless they are a known popular brand. Brand-orphan mentions are
  grouped by a 90s gap instead of spanning first-to-last mention
  across the episode (batch2 brands-listed "Made" was 155–729s,
  signed start −374s).
- V4V theme-song vanity URLs (vorac.org, morphovorek.org) are
  dropped via a `_v4vish_near` veto on `_method_url` and by requiring
  a real opening/CTA before promoting a URL-only edge block (a
  window is not "adish" just because it contains its own TLD).
- Weak directional CTAs ("why go to Google?") without a URL are not
  treated as produced edge language. URL-only edge seeds in front of
  a host outro within 90s are skipped (V4V roundtable guest-show
  plug).

72 unit tests pass.

### Official gates vs v5f (kept baseline)

| Gate | v5f | v5m | Verdict |
|---|---|---|---|
| Spliced batch1 show-notes-only coverage F1 | 44.3% (27 TP / 24 FP) | 43.2% (27 TP / 27 FP) | **near miss** (−1.1pp). The three v5l-lost TPs (Fly.io, Perplexity, Granola) are recovered. Net +3 FPs: a split Granola tail, a short `url:os` fragment, a mid-sentence-splice edge on synth-014, and a 4–25s under-cover of the real Claude preroll on synth-007 (GT 0–123, 17% coverage so it scores as FP+FN). |
| BLAB 48 coverage F1 | 38.7% (35 TP / 11 FP) | 37.4% (40 TP / 39 FP) | **within ~2pp soft budget** (−1.3pp). Recall up (25.9%→29.6%), precision down (76.1%→50.6%). Signed start −3.54s → −8.06s. mean \|start\| 20.04s → 19.68s. |
| V4V clean FP time | 1189s (1.8%, 18 blocks, 1.00/h) | 1201s (1.9%, 18 blocks, 1.00/h) | **essentially flat** (+12s). The +12s is noagenda-1913's existing `knowaginemetops` block ending 10440 vs 10428. Intro "what's up" prerolls and vorac/morphovorek/roundtable outro FPs from v5l are gone. CTA FP time improved 699s→656s. |
| Snap diagnostic (after, mid / overshoot / mixed) | v5l: 5.44 / 8.78 / 4.86s | 5.44 / 8.78 / 4.86s | **PASS** (identical, well within 0.3s). |
| DAI overall F1 / recall / signed start | 50.0% / 47.1% / −23.86s | 77.3% / 100% / −10.57s | **PASS**. Precision 53.3%→63.0% (still pessimistic). mean \|start\| 28.45s → 13.98s. |
| batch2 show-notes-only coverage F1 | 33.9% (19 TP / 18 FP) | 40.0% (24 TP / 21 FP) | **PASS** (+6.1pp). Per-ad hits 29/96 (30%) → 35/96 (36%). Pre-rolls 2/13 → 5/13, post-rolls 0/6 → 1/6, edge-rolls 2/19 → 6/19. |
| batch2 brands-listed signed start (overall / news / worst \|edge\|) | −11.96s / −95.7s / 373.6s | +9.92s (per-ad +4.0s) / +3.3s / 113.3s | **PASS** (bias moved toward 0; the 374s "Made" over-growth is gone). mean \|start\| 38.14s → 16.50s. Brands coverage F1 47.5%→46.0% (same 29 TP, +4 FP) is not the gate. |

### DAI per genre (coverage F1 / recall / signed start)

| Genre | v5f | v5m |
|---|---|---|
| business | 66.7% / 67% / −3.0s | 85.7% / 100% / −3.5s |
| comedy | n/a / 0% | 50.0% / 100% / −0.3s |
| health | 66.7% / 50% / −40.3s | 100% / 100% / −19.5s |
| history | 50.0% / 33% / −17.9s | 100% / 100% / +0.2s |
| interviews | n/a / 0% | 50.0% / 100% / +4.0s |
| news | 33.3% / 33% / −75.4s | 75.0% / 100% / −29.0s |
| sports | 50.0% / 50% / −19.7s | 57.1% / 100% / −27.5s |
| true crime | 100% / 100% / −15.8s | 100% / 100% / +3.7s |

No per-genre F1 drop vs v5f. Sports start bias is slightly earlier
(the Athletic Gemini early-start is improved overall but still
present on some sports edges). Comedy F1 50% is one FP on a
same-fill-unlabeled episode (precision pessimism), recall is 100%.

### batch2 per-ad notes (v5f → v5m)

| group | v5f hit | v5m hit |
|---|---|---|
| ALL | 29/96 (30%) | 35/96 (36%) |
| tech | 14/37 (38%) | 17/37 (46%) |
| non-tech | 15/59 (25%) | 18/59 (31%) |
| business / comedy | 0/5, 0/4 | still 0 |
| history | 1/11 | 2/11 |
| news | 3/9 | 5/9 |
| pre / post / edge-roll | 2/13, 0/6, 2/19 | 5/13, 1/6, 6/19 |

### Not kept as the official baseline

v5l improved DAI (F1 79.1%, recall 100%, signed start −10.29s) and
BLAB F1 (45.9%, 51 TP) but failed spliced batch1 (F1 38.7%, lost
Fly.io / Perplexity / Granola) and V4V (clean FP 1501s, 25 blocks)
because of theme-song prerolls and URL-only outros. v5m keeps the
DAI recall win, restores those three spliced TPs, and returns V4V
to 18 clean blocks.

### Next

- Grow the synth-007 Claude preroll (GT 0–123, currently 4–25)
  through the host-read body after "we are sponsored this episode
  by…" so it becomes a coverage TP; that alone would put batch1
  show-notes-only F1 above v5f (28 TP / 26 FP ≈ 44.8%).
- Merge the leftover Granola tail (2395–2411) with 2279–2378
  (`look back on later` is mid-read, not a topic shift).
- Mid-sentence splices that glue show talk and an opening cue into
  one 100s+ sentence (synth-014 Tailscale) still mark the host
  prefix.
- batch2 notes still miss all comedy and business ads; post-rolls
  are 1/6.
- After Tuesday (Nov 17): ingest Muse's zip, score Unsubscribe
  286/287 and SN 1101, then push only when the owner says so.
