# AdChisel test log

The canonical log of every detection and transcription experiment tried
on this pipeline: why each was a candidate, how to reproduce it, what
it found, and what was decided. Read this before re-proposing a
mechanism — most ideas here were already tested and the failures are
documented with numbers, not just verdicts.

## How tests are judged

**Final goal (2026-11-06):** label each ad's beginning and end within
one second. Detecting an ad's presence is step one; the edge accuracy
is the goal.

**Decision rule (SponsorBlock A/B tests):** KEEP only if the median
absolute edge error improves AND the share of edges within 1s improves
or holds flat, with recall down no more than 0.02 and false positives
up no more than ~10%. Borderline or mixed results are NOT included;
they are reported with numbers and flagged for whether more data could
resolve them. Losers' code is removed entirely — no dead toggles.

**Standard datasets:**

- *SponsorBlock eval set (25 episodes, 107 labels, 8 shows):* labels
  are SponsorBlock `sponsor` segments for each episode's YouTube video,
  transferred onto the podcast clock by anchoring the words around each
  edge (YouTube srv3 word-level captions → spanning phrase of 8 words
  before + 8 after the edge → SequenceMatcher against the podcast's
  Whisper transcript, ratio ≥ 0.6). SponsorBlock timestamps are never
  used directly — the YouTube and podcast clocks do not line up. An
  episode enters the set only if EVERY SponsorBlock edge anchors
  (all-or-nothing rule); one unanchorable edge discards the whole
  episode. Minimum 3 SponsorBlock sponsor segments per episode.
  SponsorBlock marks host-read description sponsors only; produced
  dynamic ads have no SponsorBlock segment. A detector block matches a
  label if their overlap is ≥ 50% of the label duration. Baseline on
  this set: recall 0.636 (68/107), 39 missed, 67 false blocks, median
  edge error 7.5s, mean 14.7s, within 1s 19.9% (27/136 edges), within
  2s 31.6%, within 5s 43.4%, max 79.3s, signed start error +4.4s
  (late), signed end error −5.4s (early).
- *Owner ground truth (Unsubscribe 286, 9 ads; 287, 8 ads):* the
  owner's own hand-marked timestamps. The gold standard for
  validating mechanisms; never used to tune individual episodes.

Local working files for these tests (eval scripts, frozen variant
outputs, transcripts) are NOT in this repo — they stay in the local
working copy. This file documents methods and results only.

---

## 1. Spotify ad classifier — REJECTED (2026-11-04)

**Why:** A BERT classifier trained to spot advertising in podcast
description sentences
(`morenolq/spotify-podcast-advertising-classification`) was proposed
as a possible detector or low-confidence voter. If a ready-made model
could separate ad from content, it would save building one.

**Methodology:** Run offline on the Unsubscribe 286/287 transcripts
(only the model weights were downloaded; no further Hugging Face
contact). Scored as a binary classifier over transcript sentences:
AUC and ad-sentence recall at the operating false-positive rate, for
host-read and produced ads separately.

**Results:** AUC **0.505** — chance. Ad-sentence recall 8.5% at
5.2% false positives. No separation for host-read or produced ads.

**Conclusion:** REJECTED. Do not wire it in as a detector or voter
without fresh evidence. A description-trained classifier does not
transfer to spoken transcript.

---

## 2. SponsorBlock eval A/B — five improvements + combined — KEPT 3 of 5 (2026-11-07)

**Why:** The scaled SponsorBlock eval (25 episodes, 107 labels) showed
the baseline at recall 0.636 with median edge error 7.5s and only
19.9% of edges within 1s. Its findings pointed at five fixable
failure patterns: tail-only detection of long host reads (the detector
caught only the CTA/URL tail, missing the story opening); duplicate-only
false blocks (repeated intros, re-read quotes); the show's own domains
firing as URL evidence (e.g. grc.com in a Security Now outro);
mis-transcribed brand names the fuzzy matcher missed (Hoxhunt →
"hawkshunt"); and head-only errors (blocks ending before the closing
URL).

**Methodology:** Each improvement implemented separately behind a
toggle in `run_pipeline.py`, run against the full 25-episode set with
the pipeline's own `detect_ad_blocks` (cached transcripts, silences,
voice/reverb results reused; harness `sb_eval/ab/ab_detect.py` +
`ab_score.py`). A fresh `combined_repro` baseline run reproduced the
published baseline exactly on every metric before any variant number
was trusted. Decision rule as above.

**Results:**

| Improvement | Recall | Median | Within 1s | FP | Missed | Verdict |
|---|---|---|---|---|---|---|
| 1. Phonetic/fuzzy brands (Soundex+LCS) | 0.579 | 8.7s | 20.2% | 77 | 45 | DISCARD |
| 2. Tail-extension (same-label merge, start side) | 0.664 | 5.6s | 25.4% | 64 | 36 | **KEEP** |
| 3. Head-extension (same-label merge, end side) | 0.645 | 7.5s | 19.6% | 66 | 38 | DISCARD (wash) |
| 4. Duplicate needs corroboration | 0.626 | 7.3s | 20.1% | 45 | 40 | **KEEP** |
| 5. Exclude show-own domains from URL evidence | 0.654 | 7.0s | 20.0% | 63 | 37 | **KEEP** |
| **Combined (2+4+5)** | **0.673** | **4.9s** | **25.7%** | **36** | **35** | **KEPT** |

Details: (1) ~480 Soundex+LCS collision hits (e.g. "matter"/"material")
swamped ~72 real manglings and moved block starts 200s+ late on
Security Now (recall 0.692 → 0.423 there). (2) Extending a heat-map
block's start to an overlapping same-sponsor single-method segment's
start, stopped at the first opening cue in between; fixed the
tail-only pattern (Function on Huberman gut-health: start error
+43.6s → +0.2s) and removed the late-start bias (signed start error
+4.4s → −0.4s). (3) A wash — the systematic head-only errors turned
out to be label artifacts (the closing URL is already inside the
block; YouTube/podcast clock drift). (4) Duplicate segments leave the
single-method auto-mark list; FP 67 → 45, at a cost of one real
Vergecast ad that was duplicate-only. (5) Show-own domains derived
generically from the RSS channel link and non-sponsor description
links; Vergecast recall 0.5 → 0.583, median 15.4 → 11.4s.
Combined: recall 0.673 (72/107), median 4.9s, mean 12.5s, within 1s
25.7%, within 2s 36.8%, within 5s 50.7%, max 79.3s (unchanged — an
Eight Sleep pod-label artifact), FP 36 (−46%).

**Conclusion:** Improvements 2, 4, and 5 KEPT and applied
unconditionally in `run_pipeline.py`; 1 and 3 removed entirely. The
phonetic idea is not dead in principle — it failed ungated, and a
corroboration-gated redesign (a phonetic match accepted only when a
second method corroborates the same spot) is queued as future work.
The combined code was re-verified on the owner ground truth
(286/287) before merging.

---

## 3. Closing-URL fallback — DISCARDED, borderline (2026-11-08)

**Why:** The head-extension test (entry 2, improvement 3) left one
genuine mechanism untested on its own: when a block ends on brand
evidence and the same sponsor's bare URL is spoken within 60s after
the block end, extend the block end to that URL's sentence. Its one
verified case (Security Now 1099 Bitwarden, end error −29.9s) was
real, but the aggregate effect was never measured alone.

**Methodology:** Re-implemented alone in `run_pipeline.py` (the
head-extension code had been deleted; the reconstruction was validated
against the verified 1099 case). Post-refinement, extend the block end
to the end of the sentence containing the last same-sponsor bare URL
within 60s after the block end. Guards: no mention of a different
sponsor between block end and URL, never extend into/past the next
block, never past the 12-minute maximum. URL-to-sponsor match at
SequenceMatcher ≥ 0.70 — the 0.85 threshold used elsewhere rejects the
one verified case ("bitworn" for Bitwarden, ratio 0.75); the looser
threshold is safe here because the match is context-constrained (60s
window after an already-labeled block + different-sponsor guard).
Tested on the same 25 episodes against the combined baseline
(recall 0.673, median 4.9s, within 1s 25.7%, FP 36).

**Results:** Every headline metric flat: recall 0.673, median 4.9s,
mean 12.5 → 12.4s, within 1s 25.7%, within 2s 36.8%, within 5s
50.7%, max 79.3s, FP 36, missed 35. Exactly ONE block changed in 25
episodes: 1099 Bitwarden (end +21.4s, error −29.9 → −8.5s). Mean
signed end error −6.8 → −6.6s. Every per-show metric identical.

**Conclusion:** DISCARDED as a one-edge effect — the decision rule
requires a median improvement and within-1s held or improved, and the
entire aggregate effect is one edge out of 144. The mechanism is
genuine (one real 21.4s fix, zero cost, zero false firings) and this
is exactly the case a dataset expansion could resolve: more episodes
→ more closing-URL cases → a measurable effect if the mechanism is
real. Code removed entirely.

---

## 4. Silence+sentence snap — DISCARDED, systematic (2026-11-08/09)

**Why:** After edge refinement, block edges land on method-segment
boundaries, which are coarse. The hypothesis: the true ad boundary is
a sentence end inside a silence gap, so snapping each edge to the
nearest sentence-final punctuation that also sits in a ≥ 0.4s silence
should land edges more precisely.

**Methodology:** Post-refinement, per block, snap each edge to the
nearest qualifying point within ±15s: a word ending in `. ! ?` whose
end is followed within 0.5s by a ≥ 0.4s pause (ffmpeg silences union
inter-word gaps ≥ 0.4s). Hard constraints: start stays before the
first in-block mention, end stays after the last, never crosses an
adjacent block, start stays before end; blocks with no in-block
mention untouched; an overlap check reverts any block a snap would
make overlap its predecessor. Tested in three forms on the same 25
episodes against the combined baseline (recall 0.673, median 4.9s,
within 1s 25.7%, FP 36): (a) full snap, both edges; (b) narrow snap,
both edges, only when the move is ≤ 2s; (c) narrow snap, ends only,
≤ 2s.

**Results:**

- (a) Full snap: recall 0.692 (+0.019, 74/107), median 4.8s (−0.1),
  mean 12.5 → 12.9s, **within 1s 25.7% → 20.9% (−4.8)**, within 2s
  36.8% → 39.2%, within 5s flat, max 79.3 → 84.4s, FP 36 → 34.
  139 snap firings (82 starts, 57 ends; 486s total moved). 94 of 144
  edges changed error: 50 worse, 44 better, 50 unchanged. Within-1s
  edges 37 → 31. Largest worsenings: a Vergecast start snapped 14s
  early into preceding content; a 1097 Rayneo end snapped 12.1s late
  to a sentence end inside the read.
- (b) Narrow both-edges ≤ 2s: recall 0.673 flat, median 4.9s flat,
  mean 12.5 → 12.6s, **within 1s 25.7% → 20.8% (−4.9)**, FP 36
  flat. 51 edge moves (magnitudes 1–2s). Within-1s edges lost 7,
  gained 0 — all 7 lost are start edges, all Huberman, all nearly
  perfect before the snap (errors −0.3 to +0.4s) and snapped 1.1–1.6s
  early.
- (c) Narrow ends-only ≤ 2s: EVERY headline metric identical to
  baseline (only signed end error −6.8 → −6.7s). 19 firings; edges
  better 10, worse 3, same 131. A wash.

**Conclusion:** DISCARDED in all three forms. The diagnosis is
systematic, not noise: the nearest sentence-end-in-silence is usually
1–5s from the refined edge in either direction, so the snap adds
±2–4s of noise at exactly the scale the ±1s goal measures — it helps
far-off edges (within-2s up) and hurts near-perfect ones (within-1s
down). The narrow cap kept none of the full snap's benefit and nearly
all of its harm, proving the damage was already in the ≤ 2s moves.
The refined method-segment edge is the better estimator. More data
would NOT resolve this. Code removed entirely. The snap idea is
closed.

---

## 5. Brand-orphan expansion — KEPT (2026-11-09)

**Why:** The brand method sometimes finds a sponsor mention but cannot
expand it into a segment (no opening cue) — a "brand orphan." The
grounding analysis found 50 orphan segments across the 25 episodes,
37 overlapping a same-sponsor baseline block, and the genuine helps
were all the same pattern: long story openings where the brand is
named once and the detector only caught the tail (Manscaped on
"ai-slop": start error +52.6s; LMNT on Sydney Sweeney: −15.0s).
The question was whether orphan bounds (padding, ±45s) could extend
blocks without over-extending them.

**Methodology:** Brand-orphan segments become merge candidates in the
single-method loop, like the tail-extension merge: same sponsor only,
overlapping a heat-map block, block not already merge-extended (no
double-extension). Start extends to the orphan segment's start,
stopped at the first opening cue between segment start and block
start, never into a preceding block. End extends to the orphan
segment's end, stopped at the first return-to-content cue after the
block end, aborted if a different sponsor is mentioned between block
end and target end, never into a following block. The mention-span cap
still bounds both extensions to ±30s. Tested alone on the same 25
episodes against the combined baseline (recall 0.673, median 4.9s,
within 1s 25.7%, FP 36).

**Results:** Recall 0.673 flat (72 matched, 35 missed, 36 FP — all
flat); median 4.9 → **4.7s**; mean 12.5 → **12.0s**; within 1s 25.7%
→ **26.4%** (+1 edge); within 2s 36.8% → 37.5%; within 5s 50.7% →
52.1%; max flat 79.3s. 25 firings; per-edge accounting: 3 edges
better, **0 worse**, 141 unchanged. Largest fixes: Manscaped on
ai-slop +52.6s → −3.9s; LMNT on Sydney Sweeney −15.0s → −0.8s;
BLUEPRINT on ai-slop +40.0s → −28.4s. The different-sponsor guard
fired correctly in analysis (AG1 end extensions blocked by Eight
Sleep / Helix Sleep mentions — the next ad in the pod). The key
mechanism insight: edge refinement undoes most of the predicted
padding harm — after the orphan extension moves edges outward,
refinement moves them back inward to the nearest real method-segment
boundary, so the extension mostly gives refinement a wider interval
to search, and only the genuine tail-only cases survive.

**Conclusion:** KEPT — the first keeper since the original three
winners. It passes the decision rule on the letter (median improves,
within-1s improves, recall and FP flat) and the gains are not noise:
the 3 fixed edges are large, systematic fixes of exactly the targeted
failure pattern, with zero offsetting harm across 25 firings. The
aggregate gain is small (median −0.2s, +1 within-1s edge) because the
pattern is rare (3 of 72 matched labels) — a modest keep, not a
breakthrough. Toggle removed, merge unconditional. Re-verified on the
owner ground truth (286/287 unchanged) and synced to the repo.

---

## 6. 20-second heat-map floor — DISCARDED, systematic harm (2026-11-09)

**Why:** The ad-length analysis of the 107 SponsorBlock labels found
median 80.5s, but 31% under 60s, 17% under 45s, 8% under 30s — and
recall on ads under 45s was 0.50 (9/18), under 30s 0.33 (3/9). The
question was whether the 30s heat-map floor (single-method floor 45s)
was throwing away real short ads.

**Methodology:** Lower `HEATMAP_MIN_AD_LEN` from 30s to 20s via a
temporary kwarg on `detect_ad_blocks`; `MIN_AD_LEN` stays 45s,
`MAX_AD_LEN` stays 12 minutes, nothing else changes. Tested alone on
the same 25 episodes against the brand-orphan baseline (recall 0.673,
median 4.7s, within 1s 26.4%, FP 36).

**Results:** Recall 0.673 flat (72 matched, 35 missed); median 4.7s
flat; mean 12.0 → 12.3s; within 1s 26.4% flat; within 2s 37.5% flat;
within 5s 52.1% → 51.4%; max flat 79.3s; **FP 36 → 49 (+36% — far
over the ~10% allowance)**. Recall on labels under 30s flat 0.33
(3/9); under 45s 0.50 → 0.56 (9/18 → 10/18). Exactly ONE more short
ad found: a 30.5s Vergecast spot. And one LONG ad lost: Security Now
1103, a 188.5s Hoxhunt read — the lower floor let the heat map split
it into three short blocks (440–460s, 490–511s, 570–616.4s), none
reaching 50% overlap of the label. Of the 6 missed ads under 30s it
found **none**. Twelve of the 13 new false blocks are 11.6–26s: the
lower floor admits short 2-vote coincidences — reverb/voice dry
islands and url/voice pairs — as blocks.

**Conclusion:** DISCARDED. Median did not improve, within-1s flat,
FP up 36%, mean and within-5s worse, recall flat — it fails the
decision rule outright. The short-ad gain (+1 label) is offset by the
long-ad loss, and the floor's guilt is settled the other way: the
evidence for the missed short ads simply is not there, not the floor.
More data would NOT resolve this: the harm mechanism (short 2-vote
coincidences admitted as blocks, plus fragmentation of long ads) is
systematic. Both floors stand: 30s heat-map, 45s single-method.
Change removed entirely; the revert was verified to reproduce the
brand-orphan baseline exactly.

---

## 7. Dynamic-slot alignment prototype — WORKS, integration undecided (2026-11-09)

**Why:** The pipeline's combined baseline gets 25.7% of edges within
1s on the SponsorBlock set, and the ±1s goal needs a fundamentally
better boundary source for dynamic (produced) ads. The observation:
downloading the same episode twice serves *different* dynamic ads on
each download, so the two copies are identical except at the ad slots.
Lining up the two transcripts should reveal the slots directly, with
edges set by the transcript difference itself rather than by detection
evidence.

**Methodology:** Download the same episode twice (Megaphone serves
different dynamic ads per download), transcribe both with the
pipeline's own `transcribe_chunked` (base, int8, CPU, 180s chunks),
align transcripts: normalize words (lowercase, strip punctuation);
anchors are 5-word shingles occurring exactly once in each transcript,
longest increasing subsequence = monotone anchor chain; gaps diffed
with difflib. Slot detection (v5b, final): per-word "differs" flags
from the gap diffs; a 40-token sliding-window density ≥ 0.6 marks
coarse stretches; boundaries refined — starts by density extension
(10-token window ≥ 0.5), ends by token-diff extension (stop at 3+
consecutive matching tokens = content resumption). Minimum slot 8s.
The hard part was transcription noise: two base transcriptions of
identical audio disagree on 8–12% of words, so naive word-diff
produced 110–130 false slots per episode; the density filter
separates real slots (~95% of words differ) from noise (~11%
scattered). Method frozen at v5b before the 287 confirmation run.
Scored against the owner ground truth for Unsubscribe 286 and 287
(dynamic ads only; host-read ads are identical in both downloads and
cannot produce a slot, by design).

**Results (combined across both episodes):** **7 of 7 dynamic ads
found, mean edge error 0.43s, 13 of 14 edges within 1s, worst edge
1.6s** — the first method tested that lands inside the ±1s goal.

- 286 (4/4): Progressive 0–56 (+0.0/+1.0), Got Junk 1421–1452
  (−0.1/+0.7), Ozempic 2419–2449 (+0.0/−1.6), Leaf Filter 3589–3620
  (−0.1/+0.4). Mean 0.5s; 7 of 8 edges within 1s, all 8 within 2s.
  Copy B is 10.7s longer than copy A; its opening ad is a Universal
  Pictures "Fockers" movie promo instead of Progressive — the slot
  mechanism working. 3 false slots: the "cha" hallucination loop at
  the Ghostbed opening, one stretch of excited speech transcribed
  differently, one dense-noise stretch.
- 287 (3/3, method frozen): Progressive 0–57 (+0.0/+0.0, exact),
  Leaf Filter 1571–1600 (+0.6/+0.9), Leaf Filter 2651–2710
  (−0.1/+0.5). Mean 0.35s; all 6 edges within 1s; **zero false
  slots**.
- All 10 host-read ads across both episodes correctly NOT found
  (identical audio in both downloads — by design; the existing
  detector still handles host-read).

Cost: one extra download + one extra transcription per episode
(~50 minutes CPU per 2-hour episode); the alignment itself takes
~1 second. Caveats: small sample (7 ads, 2 episodes, 1 show,
Megaphone only — untested elsewhere); slots still need a sponsor
name attached (the existing detector or the B-side ad copy supplies
it).

**Conclusion:** WORKS. Prototype only — no pipeline changes, no
auto-marking, no publishing. The recommended use is as a boundary
source for dynamic ads, not a standalone detector, with ad evidence
(a brand, CTA, or URL) required on at least one side of a slot before
marking — the existing detector would reject all three 286 false
slots on that test. Integration design is UNDECIDED: how the second
download/transcription fits the paused pipeline, and which download's
audio gets published.

---

## 8. Transcription benchmark — large-v3-turbo wins; switch undecided (2026-11-09)

**Why:** Two independent base transcriptions of the same audio disagree
on 8–12% of words. Part of that is chunk-boundary context (fixed 180s
chunks fall at different places when two downloads differ in length),
part is base-model error ("hawkshunt" for Hoxhunt; "cha" repeated ~150
times over a "chug chug" sound effect). Everything downstream —
detection, edges, alignment — reads the transcript, so transcription
accuracy is the ceiling on all of it. The 2026-11-06 transcription
decision had tested greedy decoding, whisper.cpp, and the tiny model
and kept base — but larger models going *up* had never been tried.

**Methodology:** 30-minute slice of Unsubscribe 286 (800–2600s,
covering the Ghostbed "cha" loop ~900–915s, Got Junk, Ozempic),
transcribed in the same 180s chunks. Reference standard: large-v3
(the most accurate Whisper model; no human transcript exists).
Candidates: base as-is (control, reused from the episode's existing
chunk transcripts), base + 10s chunk overlap, base + VAD
(silero-vad, bundled), base + hotwords (the episode's sponsor names
as hotwords/initial prompt), small, medium, large-v3-turbo. Metrics
per candidate: word agreement vs the reference (difflib on normalized
words), sponsor-name accuracy (4 in slice), hallucination loops (same
word ≥ 8×), runtime (minutes CPU per minute of audio, and implied
time for a full 2-hour episode). Models were downloaded first as a
download-only step, then all runs offline. The winner then got a
full-episode test (all of 286) to confirm the slice result at scale.

**Results (slice, agreement with the large-v3 reference):**

| Setup | Agreement | Sponsor names (4) | Loops | Time per 2h episode |
|---|---|---|---|---|
| Base (control, current) | 76.7% | 3/4 — Manscaped → "manscape" | "cha" ×222 (27s) | ~32 min |
| Base + 10s overlap | 80.9% | 2/4 — Got Junk missing | "-chuk" ×11, ×10 | ~41 min |
| Base + VAD | 79.5% | 3/4 — Manscaped mangled | "no," ×9 | ~37 min |
| Base + hotwords | 79.8% | **4/4** | none | ~38 min |
| Small | 83.5% | 3/4 — Ozempic → "osempic" | none | ~76 min |
| Medium | 80.0% | 4/4 | none | ~3h24m |
| **Large-v3-turbo** | **89.6%** | **4/4** | "jugga" ×8 (tiny) | ~2h29m |
| Large-v3 (reference) | 100% | 4/4 | none | ~6h10m |

Two takeaways: base disagrees with the best model on roughly 1 word
in 4 — the 8–12% disagreement between two base runs is only part of
the story. Medium is a dead end: less accurate than turbo *and*
slower. Overlap gained accuracy but lost Got Junk entirely; VAD gained
little (podcast audio has little non-speech to skip).

Full-episode test (turbo on all of 286, 40 chunks, 22,617 words):
runtime **150 minutes**, matching the slice prediction of ~149
almost exactly. All **9 sponsor names correct**. Agreement with the
base transcript 76.3%, matching the slice figure. The "cha" loop is
gone; in its place turbo writes "-jugga" 23 times over 4 seconds at
the same sound effect, plus two small repeats ("you" ×8 at a chunk
seam, "bye." ×19 in the outro sign-off) — all localized and harmless
for detection, versus base's single 222-word loop.

Follow-up, turbo+hotwords combo slice test (2026-11-10): the combo
scored **83.85%** — WORSE than turbo alone (89.57%) by 5.7 points.
Brands still 4/4, loops none, but the combo drops two whole stretches
of speech (49 words at 1160–1172s, 43 words at 1247–1260s). Hotwords
help base (76.7% → 79.8%, brands fixed, "cha" loop killed) but hurt
turbo: the bias distorts an already-accurate decoder. The combo idea
is stopped; turbo alone is the switch candidate, and hotwords stay a
base-only companion.

**Conclusion:** Recommendation is to switch transcription to
large-v3-turbo alone, accepting ~2.5h per episode (a background job
that processes episodes one at a time). NOT YET APPROVED — the switch
is the owner's call. The cheap fallback if that runtime is too much:
keep base and add hotwords (nearly free at ~38 min/episode, all brand
names fixed, "cha" loop gone). No pipeline changes have been made;
the benchmark was test-only. The open question is whether better
words buy better edges — see entry 10, the end-to-end check.

---

## 9. Trademark coverage check + list build — WORTH BUILDING; refresh rule set (2026-11-09)

**Why:** The owner asked whether a government list of all US companies
could verify heard brand names: a similar name "could be assumed
incorrect and fixed," and a name not on the list at all "is given a
weaker vote on the heatmap since it may not be a real brand." No single
federal list of all US companies exists (registration is state-level),
so the USPTO trademark database was the candidate: trademarks are
brand names (what sponsors are advertised by), free bulk download,
~12.7M records. Two risks flagged up front: brand names ≠ company
names (AG1 vs Athletic Greens), and the weaker-vote rule would punish
real sponsors the list misses (new/small/foreign/unregistered brands).

**Methodology:** Coverage check first, before any bulk download: a test
set of 60 real brand names (from the pipeline's own data — SponsorBlock
labels, 286/287 ground truth, description sponsors, popular-brands
list) + 10 mis-heard forms, queried against the USPTO's own Trademark
Search backend (keyless, ~70 queries in ~5 min) — live exact-phrase
wordmark query, then a spaceless variant for multi-word brands, then
an any-status query to distinguish "no mark at all" from "only dead
marks." Then the list build: USPTO Trademark Case Files Dataset 2023
(`case_file` table, 12,691,941 records, snapshot through March 2024),
downloaded keyless from the Zenodo mirror (USPTO's own routes failed:
portal shell / 403 without an API key), filtered to live marks only,
word marks only, deduplicated to distinct normalized names, name-only
storage, no age filter (old brands still advertise; live status is the
right filter), names over 80 chars dropped as slogans. Stored as
SQLite, ~0.1ms lookups. Verified by re-running the coverage test set
against the local list.

**Results:** Coverage check: **56/60 real brands have a live trademark
(93%)**; **57/60 (95%)** allowing a "THE"-prefix; ground truth 14/14
with that rule; description sponsors 39/41 (95%). **Correction signal
perfect: all 10 mis-heard forms absent.** Only 2 genuinely absent
brands: Origin HQ and Club TWiT ("joindeleteme" is a URL, not a brand;
DeleteMe itself is found). Caveats: generic-word brands (SHELL,
SIERRA, NEON) have live marks, but a mark cannot prove a heard generic
word is that brand — the weaker vote must stay a weak signal, never a
block; matching must be exact/phrase-based, not "contains" (a
contains-match would falsely cover Origin HQ off 816 "ORIGIN" marks).
List build: **2,988,549 distinct live names** (3,995,391 live serials),
**358 MB** SQLite. Local re-verification: 55/60 strict, 56/60 with
THE rule, ground truth 14/14, mis-heard forms absent 10/10 — the one
gap is "ultra pouches", filed 2025-09-10, after the March 2024
snapshot. Two build fixes along the way: draw codes are 4 characters,
not 1 (first build kept only 127 rows); five post-1990 dead status
codes were missing from the 1990 list (rebuild removed 17,966 dead
rows).

**Conclusion:** WORTH BUILDING — both halves viable (correction, and
the weaker vote as a weak signal). The list lives locally at
`~/workspace/podcast-ads/trademark_list/trademarks.sqlite` and is NOT
in this repo (358 MB of derived data; the build method is documented
here so it can be rebuilt). The list is a March 2024 snapshot and
drifts until refreshed. **Refresh rule (owner decision 2026-11-09):
do NOT use the USPTO search backend for large-scale refreshes**
(bulk extraction through a search interface, not the sanctioned bulk
route — stopped mid-run at the owner's direction); refresh only via
the sanctioned bulk download or the daily XML files. Decision: wait
for the annual bulk snapshot — brands that are simultaneously under a
year old, advertising on a podcast, AND mistranscribed are a very
small number. A monthly watch checks for a new snapshot and reports
it; it never downloads on its own.

---

## 10. End-to-end transcription check — COMPLETE (2026-11-10)

**Why:** the transcription benchmark (entry 8) proved turbo's words are
better (89.6% agreement vs 76.7% for base). This check asked whether
better words buy better 1-second ad edges — the thing the switch was
proposed to serve.

**Method:** the pipeline's own `detect_ad_blocks`, unmodified, run on
the base full transcript (22,405 words) and the full turbo transcript
(22,617 words) of Unsubscribe 286, with every other input identical
(sponsors, own domains, silences, chapters, same mp3). Scored
edge-by-edge against the owner's 286 ground truth (9 ads, 18 edges):
recall, signed per-edge error, % within 1s/2s, median/mean/worst error,
false blocks, over-mark, coverage. Harness:
`transcribe_bench/end_to_end/run_e2e.py`, results in
`end_to_end/results.json`. Detection is deterministic — a fixed re-run
reproduced both block lists exactly.

**Results (base vs turbo):**

- Recall: 9/9 both. False blocks: 0 both.
- Edges within 1s: 3/18 (16.7%) both. Within 2s: 8/18 (44.4%) base,
  6/18 (33.3%) turbo.
- Median edge error: 2.8s base, 4.5s turbo. Mean: 4.7s base, 6.2s turbo.
- Worst edge: 15.9s (NOBL start) base, 19.2s (Perfect Jean start, −19.2)
  turbo.
- Coverage: 606.8/673s base, 620.3/673s turbo. Over-mark: 17.8s base,
  58.9s turbo.
- Edge by edge: 5 better, 5 worse, 8 unchanged — the worsenings are
  much larger than the improvements. Biggest moves: Perfect Jean start
  +11.0s → −19.2s, Got Junk end +0.7s → +9.5s, NOBL end −1.9s → −12.0s,
  Ghostbed end −3.3s → +5.3s. Best improvement: Ozempic end −2.6s →
  +0.9s. Manscaped and Leaf Filter edges identical in both runs.

**Why (verified by method-level diagnosis):** edges are set by
method-segment boundaries and closing-sentence trims, not by overall
word accuracy. A cleaner transcript surfaces *more* true brand evidence
— including outside the ad. Turbo hears "Perfect Jean" 19 times
starting 34s before the ad (the host mentions the jeans in the lead-in
story), so a brand-orphan segment fires there and the block starts
19.2s early; base never heard those mentions. Turbo also hears the full
Got Junk ".com" read, extending the URL evidence 9.5s past the true end.

**Conclusion: DO NOT SWITCH to turbo on edge grounds.** The better
transcript does not buy better edges on this episode, while costing
~2.5h per episode instead of ~32 minutes. Turbo's real advantages (all
9 brands spelled correctly, no "cha" loop, cleaner words overall) are
genuine but non-edge benefits; whether they justify the runtime is the
owner's call. Caveat: one episode, 9 ads, 18 edges — a small sample,
but the direction (no gain, some harm) is clear. Next candidate: the
cheap fallback — base + hotwords (~38 min/episode) tested end-to-end
the same way; it fixes the exact defects that motivated the switch
(Manscaped mangling, "cha" loop).

### Update — base+hotwords end-to-end, and turbo+hotwords combo (2026-11-10)

**Method:** same harness, extended with a third candidate. Full
base+hotwords transcript of 286 (21,596 words, 37.7 min wall, 40 chunks,
no reboot). Base and turbo re-runs reproduced exactly, so the
comparison is anchored. Results in `end_to_end/results.json`.

**Turbo+hotwords slice test (step 1):** the one combination never tested
— turbo with sponsor-name hints together on the 30-minute slice —
scored 83.85% agreement vs 89.57% for turbo alone. It drops whole
stretches of speech and writes "Manscape" for Manscaped. Combo stopped;
turbo alone stays the accuracy candidate.

**Transcription checks (full 286):** all 9 brands correct in-window —
Manscaped 7x "manscaped", 0x "manscape" (base is the reverse: 0/7). No
mangled forms anywhere. Longest repeat "blah" x7 (below the >=8 loop
threshold); "cha" count 0 (base: x222). Agreement vs base control
90.16%.

**Results (three-way):**

- Recall: 9/9 all three. False blocks: 0 all three.
- Edges within 1s: 3/18 (16.7%) all three. Within 2s: 8/18 (44.4%)
  base, 7/18 (38.9%) hotwords, 6/18 (33.3%) turbo.
- Median edge error: 2.8s base, 2.8s hotwords, 4.5s turbo. Mean: 4.7s
  base, 5.0s hotwords, 6.2s turbo.
- Worst edge: 15.9s (NOBL start) base and hotwords, 19.2s (Perfect
  Jean start) turbo.
- Coverage: 606.8/673s base, 600.7/673s hotwords, 620.3/673s turbo.
  Over-mark: 17.8s base, 17.7s hotwords, 58.9s turbo.
- Edge by edge vs base (18 edges): 4 better, 3 worse, 11 unchanged.
  Better: Progressive end −3.0→−2.6, Ghostbed start +11.1→+8.9, Got
  Junk end +0.7→+0.6, Ozempic start +4.3→+1.4. Worse: Ozempic end
  −2.6→−3.0, SuperPower start +1.3→+2.4, NOBL end −1.9→−12.0 (same
  −12.0 as turbo — both hear something at the NOBL close that base does
  not; the one edge that moves the mean).

**Conclusion: RECOMMEND switching transcription to base+hotwords.**
Edges essentially match base — median, worst, over-mark, and within-1s
identical; within-2s 7 vs 8; mean slightly worse (5.0 vs 4.7) on the
single NOBL-end edge — while keeping every transcription fix (all 9
brands spelled right, "cha" loop gone) at ~38 min/episode vs base's
~32. Unlike turbo (worse edges at ~5x cost), hotwords costs almost
nothing and fixes real defects. Switch is the owner's call; not yet
approved. Caveat: one episode, 9 ads, 18 edges; the NOBL-end move
bears watching on a second episode. Suggested next step: confirm the
NOBL end on a second episode (Security Now), then the
corroboration-gated phonetic redesign.

---

## 11. Parakeet TDT 0.6B v3 transcription test — COMPLETE (2026-11-10)

**Why:** suggested by an outside model (Grok) as a candidate for higher
accuracy transcription. Parakeet is a NeMo model, not a Whisper model.

**Method:** same 30-minute slice of Unsubscribe 286 (800–2600s) and same
scoring as the benchmark (entry 8): word agreement vs the large-v3
reference, sponsor-name accuracy, hallucination loops, runtime. v3 runs
transformers-native on CPU (torch 2.15.0+cpu, no NeMo install needed);
model loads in 23s. Two setup fixes along the way: the Parakeet feature
extractor needs `librosa` (absent from the environment, installed), and
the driver's word-splitting assumed SentencePiece markers while v3's
tokenizer is BPE-style (a leading space marks a word start) — the first
smoke run had merged a whole 30s clip into one word. Word timestamps
work natively via the TDT duration head (verified per-word spans). v2
not tested: it is the older model, would need the heavy NeMo install,
and v3 already trails turbo by 8.1 points, so it could not change the
verdict.

**Results:** agreement **81.5%** (base 76.7%, hotwords 79.8%, small
83.5%, turbo 89.6%). Brands scored 4/4 by the flat scorer, but a
token-level check shows that overstates it: Manscaped comes out
"Manscape" (no d) in 4 of 5 standalone mentions — the same defect as
base; turbo spells it correctly 7/7. Ghostbed mostly right with one
"ghostbad.com" slip; Got Junk and Ozempic clean. Loops: none — the chug
sound effect comes out as short "chicka" bursts, never a run, vs base's
"cha" ×222. Runtime ~109 min per 2h episode (faster than turbo's 149,
slower than small's 76 and base+hotwords' 38).

**Conclusion: NOT worth pursuing.** It loses to turbo on accuracy and
brand spelling, and it is slower than base+hotwords (109 vs 38
min/episode) for only +1.7 points over hotwords — while hotwords fixes
Manscaped and Parakeet does not. And since the end-to-end check (entry
10) showed even turbo's better words do not buy better edges,
Parakeet's weaker words have no edge case either. Not a switch
candidate, not a useful complement.

---

## 12. Majority-vote transcription (3-run base) — COMPLETE (2026-11-10)

**Why:** the owner's idea — two base transcriptions of the same audio
disagree on 8–12% of words, so the errors are not in the same place
every time. Running the fast base model 3 times with different chunk
offsets and letting the transcripts vote on each word might reach
turbo-level accuracy more cheaply than a single slow pass.

**Method:** same 30-minute slice of Unsubscribe 286 (800–2600s), 3
base runs (int8, CPU, beam 5, 180s chunks) with chunk grids offset by
0s, 60s, and 120s. The three transcripts were aligned word by word
(diff-based alignment, reusing the dynamic-slot approach) and the
majority word taken at each position; ties (all three disagree) decided
by the middle-offset run; positions heard by only one run dropped.
Timestamps taken from the run that supplied the winning word. Scored
with the same metrics as the benchmark (entry 8). One aligner bug found
and fixed during the test: the first version split substitutions into
separate delete/insert columns, so contested words were dropped as
singletons and the tie-break could never fire — the fix pairs replace
spans 1:1 positionally. The 5-run variant was conditional on the 3-run
vote reaching ≥85% agreement or within ~4 points of turbo; it did not,
so it was not run (and would have cost ~205 min/episode, more than
turbo).

**Results:** agreement **81.0%** (single base 76.7%, hotwords 79.8%,
small 83.5%, turbo 89.6%) — a **+4.3 point** gain over one base run.
Brands 3/4: Manscaped still "manscape" — the systematic mishearing
survives voting, as predicted. Loops: none by the metric, and the
"cha" loop is a chunk-window artifact — none of the three offset runs
has it at all; the voted transcript still has a 9-word "jucka" repeat
at the same sound effect, missed by the loop metric because the
punctuation varies. The three runs disagree with each other on ~15% of
words (pairwise agreement 84.6–85.9%). Vote stats: 6,032 aligned
positions — 5,260 unanimous, 305 majority-2, 133 ties (2.2%), 334
singletons dropped (5.5%). Runtime ~123 min per 2h episode (more than
the naive 3×32 estimate — the offset runs each cost more than the
control).

**Conclusion: the mechanism works, but it is not a cheaper path to
turbo-level accuracy.** Turbo beats it on accuracy at similar cost
(89.6% vs 81.0%, 149 vs 123 min/episode), and base+hotwords beats it
on cost and brands (79.8%, 4/4 brands, no loops, 38 min/episode). As a
base-model strategy it does not close the gap to turbo. The technique
itself is not dead — it could layer onto other models — but it is not
the switch.

---

## 13. Base+hotwords end-to-end check — COMPLETE (2026-11-10)

**Why:** entry 10 showed turbo's better words do not buy better edges,
and entry 8 showed sponsor-name hints (hotwords) fix base's two worst
defects cheaply — brands spelled right, the "cha" loop killed — at
~38 min/episode. This check asked whether base+hotwords keeps base's
edges while keeping those fixes, making it the better switch.

**Method:** same harness as entry 10
(`transcribe_bench/end_to_end/run_e2e.py`, results extended in
`end_to_end/results.json`). Full Unsubscribe 286 transcribed with base
+ sponsor hotwords (21,596 words), then the pipeline's own
`detect_ad_blocks`, unmodified, run on that transcript with every
other input identical to the base/turbo comparison. Scored edge by
edge against the owner's 286 ground truth (9 ads, 18 edges). The
hotwords list is the episode's description sponsors. Brand/loop claims
verified separately by `end_to_end/check_hotwords_full.py`.

**Results (base vs hotwords vs turbo):**

- Recall: 9/9 all three. False blocks: 0 all three.
- Edges within 1s: 3/18 (16.7%) all three. Within 2s: 8/18 (44.4%)
  base, 7/18 (38.9%) hotwords, 6/18 (33.3%) turbo.
- Median edge error: 2.8s base, 2.8s hotwords, 4.5s turbo. Mean:
  4.7s base, 5.0s hotwords, 6.2s turbo.
- Worst edge: 15.9s (NOBL start) base and hotwords, 19.2s (Perfect
  Jean start, −19.2) turbo.
- Coverage: 606.8/673s base, 600.7/673s hotwords, 620.3/673s turbo.
  Over-mark: 17.8s base, 17.7s hotwords, 58.9s turbo.
- Edge by edge, hotwords vs base: 4 better, 3 worse, 11 unchanged.
  Best move: Ozempic start +4.3s → +1.4s. Worst move: NOBL end
  −1.9s → −12.0s — turbo makes exactly the same move, so both hear
  something at the NOBL close that base does not.
- Transcription fixes hold on the full episode: Manscaped spelled
  right 7/7 in the ad window (base: "manscape" 7/7), all 9 brands
  present and correct in their ad windows, "cha" count 0 (base: 222).
  Longest same-word run in hotwords: "blah" ×7.
- Runtime: ~38 min per 2h episode (base ~32, turbo ~149).

**Conclusion: base+hotwords is the recommended switch.** Unlike
turbo, which gives worse edges at about five times the cost, hotwords
costs almost nothing and fixes real defects — brands spelled right, no
"cha" loop — while the edges stay where they are (median identical,
mean +0.3s, driven by the single NOBL-end move). Caveats: one episode,
9 ads, 18 edges — a small sample — and the NOBL-end move bears
watching on a second episode. **The switch itself is the owner's call;
no pipeline change made.** Suggested next step if approved: confirm
the NOBL end on a second, calmer episode (Security Now) before
treating the switch as settled.

**Implementation attempt — REVERTED (2026-11-10, same day the switch
was approved).** The switch was implemented in `run_pipeline.py`
(sponsors extracted from the description before transcription, passed
as hotwords) and verified twice against this entry's targets; both
attempts failed and the code was reverted to its exact pre-switch
state (verified: compiles clean, zero hotwords references). Attempt 1
(ALL-CAPS description labels): recall 5/9, 1 false block, coverage
370.0/673s. Attempt 2 (title-cased labels): recall 8/9, 1 false block,
coverage 513.5/673s — the Perfect Jean ad missed entirely, false
block "THE PERFECT JEAN" at 1173–1251s. Root cause, verified in the
faster-whisper source: hotwords are prepended to the decoder prompt as
previous context, and on ambiguous chunk starts the decoder echoes the
prompt instead of transcribing — whole chunks collapse to repeated
hotwords (up to ~90% word loss). Two gaps vs the tested configuration:
the pipeline can only derive 5 description sponsors, but the test used
9 hand-listed names including 4 dynamic-ad brands absent from the
description; and description labels are ALL-CAPS vs the test's Title
Case. Diagnostic lead, single-chunk probe only and untested
end-to-end: dropping "The" from the hotwords string restores the
collapsed chunk to normal speech. A third full verification with that
variant needs the owner's say-so. One keeper from the attempt: the
per-chunk `transcribe_config` fingerprint (re-transcribe on config
mismatch) proved working — all 40 stale caches detected — and is worth
re-adding with any future switch. Full record in local HANDOFF.md;
verification artifacts under `tmp/verify_hotwords_switch/`.

**Third verification — FAILED, switch abandoned (2026-11-10,
owner-approved).** Hotwords `Ghostbed Manscaped Nobl Superpower
Perfect Jean` (attempt 2's title-cased labels with "The" dropped).
Chunk 6 clean as the probe predicted, Perfect Jean found, 0 false
blocks — but Ghostbed missed instead: recall 8/9, within 1s 4/18,
within 2s 8/18, median 2.2s, mean 4.9s, over-mark 18.7s, coverage
548.1/673s. The Ghostbed opening sound effect looped again in altered
form — "tu" ×12, only 2 "ghostbed" mentions surviving in the ad
window — so the block covered 36% of the ad, below the 50% match
threshold. Pattern across all three attempts: each hotwords wording
fixes one chunk and breaks another; the prompt wording decides
loop-or-not per chunk in ways that are not predictable. Conclusion:
the hotwords mechanism itself is unreliable here, not one bad string.
Transcription stays plain base; `run_pipeline.py` reverted to its
exact pre-switch state (verified: compiles, zero hotwords references).
Result in `tmp/verify_hotwords_switch/verify_result3.json`.

---

## 14. Corroboration-gated phonetic redesign — DISCARDED, all three gates (2026-11-10)

**Why:** the first phonetic test (entry 2, improvement 1) failed on
~480 Soundex+LCS collisions vs ~72 real manglings. The redesign
accepted a phonetic match only when a second source corroborated the
same spot. The trademark list (entry 9) existed by then to serve as a
gate, and the owner's web-search verification idea was a second
candidate gate.

**Method:** the imp1 phonetic gate was rebuilt fresh (its code had
been deleted): 3-digit Soundex over all letters + longest-common-
substring ≥2 + first-letter/LCS conditions, plus the missing
constraint |length difference| ≤ 2 — without it the reconstruction
gives 1,928 hits instead of imp1's 550; with it, 568 hits. Baseline
reproduced exactly first (`combined_repro7`, all deltas 0.0 vs
`brand_orphan`). Three gates tested alone on the same 25 episodes:
- **phonetic_ev (evidence gate):** a phonetic hit counts only if an
  exact/fuzzy mention of the same sponsor, or a bare-URL stem
  phonetically matching a variant, sits within ±60s. Window
  sensitivity checked (±30/60/120/180s); ±60s chosen — widening to
  120s gains 2 missed-label hits but adds 16 content hits.
- **phonetic_tm (trademark gate):** a phonetic hit counts only if the
  heard word is NOT a live trademark. Offline it proved too blunt on
  its own: kills ~83% of known collisions but also ~half the known
  real manglings (woop, rora, rudder, plaid, jura, century ARE live
  trademarks).
- **phonetic_both:** both conditions.
- The web-search gate was not pursued: the evidence gate already
  plays its corroboration role, and the failure turned out structural
  (below), not a corroboration-quality problem.
Harness: `sb_eval/ab_detect.py` + `ab_score.py`; frozen results under
`sb_eval/ab/phonetic_ev|tm|both/results.json`; gate analysis in
`sb_eval/ab/phonetic_gate_analysis.py`, `phonetic_gate_window.py`,
`phonetic_gate_hits.json`.

**Results vs baseline (recall 0.673, median 4.7s, mean 12.0s, within
1s 26.4%, within 2s 37.5%, FP 36, missed 35):**

- phonetic_ev: recall 0.673, median 4.7s, mean 12.6s, within 1s
  26.4%, within 2s 36.8%, FP 37, missed 35, labels +1/−1.
- phonetic_tm: recall 0.636, median 4.7s, mean 12.7s, within 1s
  27.9%, within 2s 39.0%, FP 42, missed 39, labels +1/−5.
- phonetic_both: recall 0.664, median 4.6s, mean 12.2s, within 1s
  26.8%, within 2s 37.3%, FP 37, missed 36, labels +0/−1.

**Decision-rule check:** ev fails (median did not improve); tm fails
outright (recall −0.037 past the 0.02 allowance, FP +16.7% past
~10%); both is borderline/mixed (gains nothing, loses the SN 1103
Hoxhunt label) → do not include.

**Why it fails (mechanism, verified in the logs):** phonetic hits
cluster at the **tail** of a read — the only place Whisper's error is
a close spelling is the spelled-out URL ("hawkshunt dot com"); the
conversational body is transcribed as unrelated words. The hits then
flip sponsors from the orphan path to the brand path:
`_method_brand_orphan` skips any sponsor whose `_expand_segment` now
succeeds, so the wide orphan segment (mention span ±45s) that used to
carry the block disappears, replaced by the brand method's narrow
segment around the late hits. Cap and refinement then pull the block
start inward — Hoxhunt cap 540→569s, refine 569→588s (block covers
28s of a 189s label); SN 1096 Bitwarden start +263s, Material +215s;
SN 1099 Guardsquare +109s, Doppel +92s. Coverage falls below 50% and
the label is lost. The looser the gate, the worse the damage — gate
strictness is not the fix. **More data would not resolve this;
systematic, not noise.**

**The decisive number:** the within-1s edge **count is 38 in the
baseline and in all three variants** (ev 38/144, tm 38/136, both
38/142). The phonetic channel improved **zero edges**; the percentage
"gains" in tm/both are pure denominator artifacts from losing labels.
Within-2s count is 53 in all three vs 54 baseline.

**Conclusion: DISCARD all three gates.** Variant code removed
entirely from `ab_detect.py` (only a removal comment remains);
post-removal verification: SN 1103 re-run reproduces the
`brand_orphan` blocks exactly. `run_pipeline.py` never touched. A
design constraint is recorded in `STATUS_AB.md` and HANDOFF.md: any
future mechanism that adds mentions through `find_mentions` risks the
same orphan flip and must be checked against it. One further idea from
this test, for the owner's direction only (a redesign, not a tweak):
phonetic hits used **only as a vote inside already-detected blocks** —
never creating segments or flipping the orphan path.

---

## 15. Per-word heat-map — DISCARDED, borderline/mixed (2026-11-10)

**Why:** the owner asked 2026-11-09 whether per-word resolution
would beat the per-second heat map. The prior assessment was probably
not — the heat map decides *whether* an ad is here while final edges
already come from word timestamps via refinement, and per-word
counting risks fragmenting blocks on Whisper word-timestamp jitter.
This test settled it empirically. The genuinely new capability a
per-word map could enable — word-level vote counts to score
per-boundary confidence (the boundary-scorer idea) — was evaluated as
a secondary analysis.

**Method:** word-level vote bins instead of per-second bins in
`detect_ad_blocks`: a method segment covers a word iff the word's
start falls inside the segment (inclusive — the analog of the
per-second loop's inclusive second range); runs of voting words (≥2
votes) break where the gap between consecutive voting words exceeds
20s (the existing gap-merge threshold); block start = first voting
word's start, end = last voting word's end. Floors unchanged in
seconds (30s heat-map, 45s single-method). Edge refinement untouched
— one change at a time. Orphan-flip check passed at design time: the
variant changes binning only, adds no mentions through
`find_mentions`. Baseline reproduced exactly first
(`combined_repro8`, all deltas 0.0 vs `brand_orphan`). Smoke test on
SN 1102: same 6 blocks; only the 'shell' edges moved 0.6s/0.3s.
Harness: `sb_eval/ab_detect.py` + `ab_score.py`; frozen results in
`sb_eval/ab/perword/results.json` (per-episode `word_votes.json`
saved for all 25 episodes); analysis in
`sb_eval/ab/analyze_perword.py`.

**Results vs baseline (recall 0.673, median 4.7s, mean 12.0s, within
1s 26.4%, within 2s 37.5%, within 5s 52.1%, FP 36, missed 35, 144
edges):**

- Per-word: recall 0.664 (71/107), median 4.0s, mean 11.9s, within
  1s 27.5% (39/142), within 2s 38.7% (55/142), within 5s 53.5%, FP
  36, missed 36, max 79.3s (flat).
- Per-show: 99pi within-1s 83.3% → 100%; Huberman recall flat,
  within-1s 57.1% → 54.8%; Vergecast recall 0.5 → 0.417 (one label
  lost), median 6.1 → 2.5s (composition — see below).
- **Fragmentation did not happen:** heat-map blocks 107 → 108 (99pi
  2→3, Huberman true-happiness 5→4, Vergecast cloud-gaming 1→2);
  final blocks 108 → 107. The feared word-jitter splitting never
  materialized.
- **Per-edge deltas (labels matched in both runs): 4 better / 4
  worse / 134 unchanged of 142 — net edge movement zero.** The two
  genuine improvements are both start edges fixed to 0.0s (99pi
  6.8→0.0; Vergecast cloud-gaming 2.7→0.0).

**Why discard despite the headlines passing the letter of the rule:**
the rule's letter passes (median improves, within-1s improves,
recall down 0.009 ≤ 0.02, FP flat), but the gains are not real
binning gains, so this is the rule's borderline/mixed case — do not
include. The median "improvement" (4.7→4.0s) is a composition
artifact: the lost label's two edges (|err| 18.0s and 20.1s in the
baseline) left the set, mechanically lowering the median. Within-1s
count 38→39 = +2 real fixes −1 lost-label edge. The lost label
(Vergecast, 2218–2264s, Booking) shrank to a 3.9s remnant: the
word-binned block was 2200.2–2260s vs per-second 2200–2261s, but
refinement pulled the per-word start 2200→2240 while the baseline
start stayed 2200. Mechanism: `find_mentions` fuzzily matches
'looking' ~ 'booking' at exactly 2200.00s; the integer-second block
start included that mention, pinning the earliest in-block mention to
2200.00 and blocking refinement; the word-binned start (2200.2s)
excluded it, so the earliest in-block mention became the real
'booking' at 2240.22s and a method segment starting at 2240s became
the refined edge. A 0.2s start difference swung the edge 40s.
**Refinement, not binning, sets the edges** — exactly as the
2026-11-09 assessment predicted. More data would NOT resolve this:
the effect is systematic (134/142 edges identical; refinement
dominates final edges) and the loss mechanism is structural, not
noise. The per-word map as a block-formation mechanism is closed.

**Secondary finding — boundary confidence (the real lead from this
test):** word-level vote profiles **do** score edge quality.
Sharpness = mean votes of 3 words just inside the edge minus mean
votes of 3 words just outside (n=138 matched edges, Spearman rho =
−0.360): flat (<0.5): n=57, median |err| **13.5s**, mean 18.0s,
within-1s 9/57; medium (0.5–2): n=59, median |err| 2.4s, mean 8.6s,
within-1s 22/59; sharp (≥2): n=22, median |err| 2.7s, mean 6.6s,
within-1s 4/22. Sharp vs medium is indistinguishable, but flat vs
non-flat is a large split. A per-edge confidence score (flag or repair
flat edges) is feasible — a boundary-scorer input, not a
block-formation change. Not built; it needs the owner's direction.

**Conclusion: DISCARD.** Variant code removed entirely from
`run_pipeline.py` and `ab_detect.py` (zero `word_heatmap` references;
both compile). Post-removal verification: full forced re-run of
`combined_repro8` reproduces `brand_orphan` exactly (all deltas 0.0).
Pipeline back to its exact pre-test state.

---

## 16. Boundary-confidence score — FLAG KEPT, REPAIR DISCARDED (2026-11-10)

**Why:** the per-word heat-map test (entry 15) found that word-level
vote profiles score edge quality: flat edges (vote sharpness <0.5)
have median error 13.5s vs 2.4s for the rest. The owner approved
building this score. Two parts: (1) evaluate sharpness as a FLAG
for bad edges; (2) design and A/B test a REPAIR rule for flat edges
only. Context: the 2026-11-06 recommendation revision made the
learned target a boundary scorer — this score is a boundary-scorer
input.

**Sharpness definition (same as entry 15's secondary analysis):**
mean votes of 3 words just inside the edge minus mean votes of 3
words just outside, from per-word vote profiles (a method segment
covers a word iff the word's start falls inside it). Helpers
`_word_votes` / `_edge_sharpness` in `run_pipeline.py`; the profiles
reproduce the frozen perword `word_votes.json` exactly (0 diffs on
99pi and Huberman accelerate-learning). Baseline reproduced exactly
first (`combined_repro9`, all deltas 0.0 — recall 0.673, median
4.7s, mean 12.0s, within-1s 26.4%, FP 36, missed 35).

### Part 1 — FLAG evaluation (analysis scripts
`sb_eval/ab/analyze_flag.py`, `ab/analyze_flag2.py`,
`ab/analyze_repair_sim.py`; baseline matched edges, n=140 with
profiles, 4 edges at episode boundaries have no profile)

- Bucket stats on baseline edges (reproduces entry 15's finding):
  flat (<0.5) n=60, median |err| 14.3s, mean 17.8s, within-1s
  10/60; medium (0.5–2) n=59, median 2.7s, mean 8.7s, within-1s
  21/59; sharp (≥2) n=21, median 2.7s, mean 6.9s, within-1s 3/21.
  Flat vs non-flat median 14.3s vs 2.7s. Spearman rho(sharpness,
  |err|) = −0.359.
- Flat as a classifier of bad edges: |err| > 5s — precision 0.67,
  recall 0.58, F1 0.62 (base rate 0.49; 40 TP, 20 FP, 29 FN);
  |err| > 10s — precision 0.53, recall 0.63, F1 0.58 (base rate
  0.36). Genuinely informative, far from a clean separator.
- Threshold sweep (flag = sharpness < cut): 0.5 is near-optimal
  for F1 at >5s (0.62; 0.25 gives 0.63, 0.75/1.0 give 0.67 but
  flag 77 edges vs 60). The 0.5 cut stands; 0.75–1.0 is marginally
  better on F1 but flags 28% more edges. 1.5+ flags nearly
  everything (115/140) and precision collapses.
- Signed direction: flat edges skew EARLY on both sides — starts
  mean −7.9s (9 early, 2 late, n=14), ends mean −9.4s (29 early,
  10 late, n=46). Non-flat: starts mean −0.3s, ends −2.5s.
- **Sharpness at the TRUE label edges: median 0.00; 102/140 true
  edges are flat (70% of starts, 76% of ends). The vote profile
  does NOT step at the true boundary — the flag identifies bad
  edges but the profile does not locate good ones. This is the
  finding that kills the repair (below).**
- Nearest vote step to the TRUE edge: starts median 3.8s (within
  1s 20/72), ends median 2.8s (within 1s 14/72); nearest
  method-segment boundary: starts 3.6s (22/72 within 1s), ends
  2.3s (15/72). Only ~20–30% of true edges have a step/boundary
  within 1s. For the 40 flat BAD edges specifically, the nearest
  vote step to the truth is median 23.9s (starts, 1/11 within 1s)
  and 2.8s (ends, 9/29 within 1s) — start truths sit in wide
  step-free regions.
- Offline repair simulation (three rules, flat edges only): R1
  nearest step FORWARD — flat median 14.3 → 10.0s but within-1s
  10 → 6; aggregate median 4.9 → 4.7s, within-1s 34 → 30 (of
  140), mean 12.2s. R2 nearest step EITHER direction — worse on
  everything (median 5.2s, within-1s 29, mean 13.7s). R3
  direction-appropriate step FORWARD — same as R1 (median 4.7s,
  within-1s 30, mean 12.0s). The best rule trades a 0.2s median
  tick for −4 within-1s edges. The sharpness-margin variant
  (target must beat current sharpness by ≥ 1.0) is worse still:
  the qualifying step lands a median 19.9s from the truth
  (nearest-wins) / 22.2s (sharpest-wins), 2/39 within 1s — the
  margin filters the truth OUT, because the truth is flat.

### Part 2 — REPAIR, A/B tested in the harness (variant
`sb_eval/ab/boundary_repair/`, 65 firings)

Rule as implemented (`_repair_flat_edges`, since removed):
post-refinement, flat edges only (sharpness < 0.5), move to the
nearest direction-appropriate vote step FORWARD in time within
30s — start to the nearest UP step, end to the nearest DOWN step.
No sharpness margin on the target. Hard constraints (same as the
snap test): start stays before the first in-block mention, end
stays after the last, never crosses an adjacent block, block
keeps ≥ 15s; blocks with no in-block mention untouched. The
repair is driven by the vote profile / method-segment boundaries,
NOT by silence gaps alone — the silence+sentence snap test (entry
4) already showed moving edges to silence gaps adds noise.

Results vs baseline (recall 0.673, median 4.7s, mean 12.0s,
within-1s 26.4%, within-2s 37.5%, within-5s 52.1%, FP 36, missed
35, 144 edges): **boundary_repair: recall 0.701 (75/107), median
4.6s, mean 12.1s, within-1s 23.3% (35/150), within-2s 36.0%,
within-5s 52.7%, FP 33, missed 32, max 79.3 → 73.8s. Signed end
−6.8 → −3.4s** (the early-end bias halves — the forward end
moves do what they were designed to do). Per-edge deltas (labels
matched in both): better 26 / worse 19 / same 99.

**Decision-rule check: median improves (4.7 → 4.6s) and recall
rises and FP falls — but within-1s FALLS 26.4% → 23.3% (38 → 35
edges), so the rule's "median improves AND within-1s
held/improved" FAILS. VERDICT: DISCARD as borderline/mixed.**

The headline gains are not real edge gains. The 3 gained labels
are marginal-overlap matches with bad edges: Huberman motivation
(2543.5–2693.6) block 2544–2620, start +0.3 but end −73.8s;
Modern Wisdom 4-men (4472.3–4536.0) start +13.5 / end −14.0;
Modern Wisdom willpower (3366.9–3422.7) start +3.1 / end −22.7.
The within-1s losses are the real story: 7 near-perfect end edges
pushed OUT (e.g. −0.4 → +1.1, −0.4 → +4.6, −0.6 → +6.3, −0.4 →
+7.2 — all Huberman) against 3 gained. The repair "fixes" an end
edge 0.4s early by moving it to the next down-step, which sits
1–7s past the truth. Same failure shape as the silence+sentence
snap (helps far-off edges, hurts near-perfect ones), at smaller
scale. More data would NOT resolve this: the mechanism is
systematic (true edges are flat in the profile; vote steps sit
past the truth), not noise. The vote-step / segment-boundary
repair is closed. A repair would need a target that actually sits
at the true boundary — the profile does not provide one.

### FLAG — KEPT

The flag separates good from bad edges well enough to keep:
blocks are now (start, end, label, start_sharpness,
end_sharpness) from `detect_ad_blocks`, carried into the report
and state (`start_sharpness` / `end_sharpness` per block) and
into `detect.json`. Sharpness is None when the window is empty
(edge at the episode boundary — 4 of 144 edges). **Edges are
unchanged: `combined_repro10` reproduces `brand_orphan` exactly
(all deltas 0.0), and 286/287 re-verified
(`tmp/score_current_flag.log`): 286 7/9 ads (≥80% coverage),
606.7/673s, over-mark 18s, 0 false; 287 6/8 ads, 463.0/531s,
over-mark 41s, false blocks 1 (Pepperbox house plug, as before)
— identical to the pre-flag run (`tmp/score_current_orphan.log`).**
Repair code removed entirely (`_repair_flat_edges`,
`boundary_repair` kwarg and hook, variant entry — zero
`boundary_repair` references in `run_pipeline.py`). Helpers
`_word_votes` / `_edge_sharpness` stay — they serve the flag.
Downstream compatibility: four reusable drivers that unpacked
blocks as 3-tuples were updated to the 5-tuple return
(`sb_eval/transcribe_detect.py`,
`transcribe_bench/end_to_end/run_e2e.py`,
`validate_286_1101.py`, `score_287.py`); frozen one-off scripts
in `tmp/` and `sb_eval/ab/test_imp2_*` / `test_imp3_*` were left
untouched deliberately (historical records, not runnable
tooling). What the flag enables: low-confidence edges can be
surfaced in reporting (the nightly marked-ads list can note
which edges are shaky), and sharpness is a ready input feature
for the future boundary scorer (the 2026-11-06 recommendation
revision's learned target). Variant outputs frozen under
`sb_eval/ab/boundary_repair/`; baseline repros under
`sb_eval/ab/combined_repro9/` (pre-flag) and
`sb_eval/ab/combined_repro10/` (post-flag, verifies edges
unchanged).

**Conclusion: FLAG KEPT, REPAIR DISCARDED.**

---

## 17. Method-weight search — SHELVED (2026-11-11)

**Why:** PR #2 (Grok) hand-picked weights and failed badly (286
coverage 606.7→401.9s, SB recall 0.673→0.495) — the failure was in
the numbers, not the concept. The owner approved learning the
weights from the labeled data instead. Two methods: coordinate
descent from the current rule, and a logistic-regression fit as a
cross-check.

**Design:** threshold fixed at 1.0, weights on the coarse grid {0,
0.25, 0.5, 0.75, 1.0}. Voice FIXED at 0 (review-only by standing
rule, never promotes — not a learnable parameter). Floors unchanged
(30s heat-map, 45s single-method). Methods weighted are the current
pipeline methods only (chapter, brand, cta, cluster, url,
duplicate, brand-orphan, reverb, cluster-popular, voice); url-strong
is a PR invention and was not added. Scalar objective, stated up
front: matched labels − 0.5 × FP (one matched label is worth two
false blocks). True baseline: 72 matched, FP 36 → 54.0. Starting
point (all methods 0.5, voice 0): 71 matched, FP 33 → 54.5 —
fixing voice at 0 alone loses 1 label and removes 3 FPs. Candidates
computed once per episode and reused via a temporary `_candidates`
hook; a full-set evaluation took 2.6s after the first call in a
process (vs ~20s/episode for full detect), which made the search
and leave-one-show-out feasible. Baseline reproduced exactly first
(`combined_repro11`, all deltas 0.0).

**Coordinate descent (full 25-episode set, converged round 3):**
- R1: reverb 0.5 → 0.0 (matched 71→69, FP 33→27);
  cluster-popular 0.5 → 0.0 (69→69, FP 27→26)
- R2: brand-orphan 0.5 → 1.0 (matched 69→71, FP 26→28); R3: no
  improvement
- **Finalist:** chapter/brand/cta/cluster/url/duplicate 0.5,
  brand-orphan 1.0, reverb 0.0, cluster-popular 0.0, voice 0.0
- **Full-set numbers** (harness `weights_cd`, identical to
  in-memory): recall 0.664 (71/107), missed 36, FP 28, median
  4.7s, mean 11.7s, within-1s 26.1% (37/142), within-2s 35.2%
- **Decision-rule check: FAILS.** Median flat (4.7→4.7s, must
  improve) and within-1s 38→37 edges (must hold). Recall down
  0.009 (within 0.02), FP down 8. Per-episode: Acquired FP 5→0
  (the big win), Sydney Sweeney +1 label, unsub-287 −1, Vergecast
  creator −1.
- **Must-promote checks:** orphan+reverb 1.0 ✓ (via orphan
  alone); cluster+reverb 0.5 ✗; url+reverb 0.5 ✗. Orphan at 1.0
  means a lone orphan segment now promotes a block at the 30s
  floor.

**Logistic-regression cross-check — disagrees, and is worse:**
coefs brand +7.07, orphan +4.01, cluster +3.60, cta +3.28, url
+2.68, duplicate +1.68, reverb +0.49, cluster-popular +0.19,
chapter **−19.69** (chapter coverage is strongly anti-associated
with true-ad seconds — publisher chapter segments are long and
mostly content at the second level). Mapped to grid: chapter 0,
brand 1.0, cta/cluster/url/orphan 0.5, duplicate 0.25,
reverb/cluster-popular/voice 0. Block-level: matched 60, FP 19,
recall 0.561, objective 50.5 — worse than the starting point, and
it fails all three must-promote checks. The disagreement is
diagnostic: per-second fitting optimizes the wrong objective for
block promotion; weak methods that matter at block level look
useless per-second.

**Leave-one-show-out — the deciding evidence:** search re-run per
fold on 7 shows, tested on the held-out 8th. **Held-out aggregate:
matched 69/107, recall 0.645, FP 39 — vs the starting point on the
same held-out episodes 71/107, recall 0.664, FP 33.** Worse on both
recall (−2 labels) and FP (+6). Per fold: 99pi / Acquired /
Security Now / Vergecast unchanged; Huberman −1 label +2 FP; Lex
same recall +2 FP; Modern Wisdom +1 label +2 FP; Unsubscribe 8/10
(baseline 10/10), FP flat — the worst fold, on one of the two
served shows. Fold weights are unstable: cluster 0.0 (Huberman
fold) vs 1.0 (Lex / Modern Wisdom folds); reverb→0 learned only
when Acquired is in training. With 107 labels and 9 free weights,
single-weight moves flip on 1–2 labels.

**Conclusion: SHELVED (owner, 2026-11-11 — not discarded).** The
method is sound; the data is insufficient. The full-set gain was
fitted to the 25 episodes; on unseen shows the learned weights are
worse than the starting point. A retry needs more manually labelled
episodes first, not a better search on these 107 — and the retry
data must be manual labels, not more SponsorBlock (SB marks
host-read description sponsors only, so dynamic ads enter training
as negatives; SB is validation data, not training data). The fitted
weights themselves are not kept (worse on held-out shows); what is
shelved is the test, with its frozen records (`sb_eval/ab/weights_cd/results.json`,
`sb_eval/ab/search_weights.py`, `sb_eval/ab/weights_search/`) ready
for a retry.

---

## 18. Dynamic-slot integration — IMPLEMENTED (2026-11-11)

**Reason.** The dynamic-slot prototype (entry 7) was the most accurate
boundary method tested: 7/7 dynamic ads, mean edge error 0.43s, 13/14
edges within 1s. The integration design was approved 2026-11-11 and
implemented the same day.

**Methodology.** Port of the prototype's v5b density-slot aligner
(`pipeline/slot_align.py`, 325 lines; 5-word-shingle anchors, 40-token
density window at 0.6, hybrid boundary refinement, 8s minimum). The B
flow runs after A detection, before chapter building, so any B failure
degrades to detector-only output. B transcript cached in `work/B/`,
alignment in `work/align_result.json` keyed by both SHAs. Copy A is
always published, chapters on the A clock; B is never published and
its mp3 is deleted after alignment. Naming is A-side only
(detector-block label, then slot-internal brand mentions, then unnamed
`Muse Detected Ad`). Merge: a gated slot overlapping a detector block
takes the slot's edges + the detector's label, no refinement; a slot
with no detector block becomes a new chapter; slot blocks bypass the
30s/45s floors; house-plug labels are never overwritten. Gate: A-side
any method segment overlapping the slot or a sponsor mention inside
it; B-side brand mention plus a URL/CTA nearby. Probe once per
podcast (byte-compare a second download) + 30-day re-probe +
`dynamic_ads` config override. Sanity guards: differing-token rate
>25%, duration ratio >10%, slot share >25% abort slots for the
episode. Transcribe-config fingerprint re-added, covering both caches.
Standalone `--slots-only` mode for manual-marking aid. Two deviations
from DESIGN.md, both documented in code: alignment re-runs on every B
flow (~1s) rather than skip-cached; a B-cache SHA invalidation marker
was added because the design left the B transcript cache unkeyed by
audio SHA (a fresh B file would have aligned against a stale cached B
transcript) — unit-tested 3/3.

**Results — Unsubscribe 286/287 vs the owner's ground truth**
(`tmp/dynslots_val/results_286_287.json`). Detector baselines
reproduced exactly first. Merged output: 286 — ≥50% scorer 9/9 ads,
≥80% scorer 9/9 (was 7/9), coverage 645.3/673s (was 606.7), over-mark
27.0s; 287 — ≥50% 8/8, ≥80% 7/8 (was 6/8), coverage 477.3/531s (was
463.0). 17/17 ads kept at the ≥50% scorer. Dynamic-ad edges reproduce
the prototype exactly: mean error 0.42s, 13/14 edges within 1s, worst
1.6s. Overall edges improve on both episodes (286 mean |err|
4.67→3.73s, within-1s 3/18→7/18; 287 4.95→4.09s, 5/16→9/16). All 10
host-read blocks unchanged by the merge.

**Gate — one failure against the design's criterion.** 287: PASS,
3/3 true slots gated, 0 false. 286: 4/4 true slots pass, but 1 of 3
false slots passes — the 899.4–927.1 "cha" hallucination slot, which
sits inside the real Ghostbed ad, so a Ghostbed method segment
overlaps it and the A-side gate fires. It becomes an unnamed extra
block, so 286 goes from FP 0 to FP 1 at the ≥50% scorer. A
mention-based tightening would not separate them: only the Progressive
slots carry a brand mention from any list (description, house-plug, or
popular brands); Got Junk, Ozempic, and Leaf Filter are in no pool, so
requiring a mention would fail 5 of 7 true slots. Owner decision
pending: accept this FP, or tighten the gate/merge for the
partial-overlap case (a gated slot overlapping a detector block by
<50% currently becomes a standalone block).

**Results — SponsorBlock probe (25 episodes)**
(`sb_eval/ab/dynslots/probe.json`): 12 dynamic, 10 static, 3
probe-download failures (transient CDN disconnects, retried). The same
show has both kinds (Huberman 5 dynamic / 3 static; Security Now 2
dynamic / 2 static) — dynamic-ness is per-episode, not per-show.
Static episodes: blocks identical to the brand-orphan baseline (10/10
episodes spot-checked; aggregate labels 44, matched 27, recall 0.614,
missed 17, FP 17 — an exact match on the same 10 episodes).

**Security Now 1101:** detector gives 6 blocks (spans differ from the
2026-10-27 old list — pre-existing, from own-domains and brand-orphan
landing since then, not caused by this change). The probe shows the
second download differs by 303,176 bytes — the feed is NOT static, so
the B flow runs rather than the design's assumed no-op. B-flow result
pending (driver running; transcription ~5 min per 180s chunk, 0.6x
realtime — about 2x slower than the design's estimate, so the full
dynamic SB run is ~40h).

**Conclusion.** Implemented and merged into the pipeline; 286/287
gains are real and the static-episode behavior is provably unchanged.
Open: the owner's gate-gap decision, and whether the ~40h dynamic SB
validation is worth running to completion (driver
`sb_eval/ab/run_dynslots_remaining.sh` is running it: 1101 B flow,
`--slots-only` test on 286, 12 dynamic SB episodes + 3 probe-failed
retries).

---

## 19. Spectral-step splice evidence (MinusPod idea 1) — DISCARDED at Phase A (2026-11-11)

**Reason.** MinusPod's splice detector measures the change in
spectral centroid/flatness across a silence — a splice point between
separately mastered audio. AdChisel's flat-edge problem (true edges
are themselves flat in the vote profile, entry 16) made this the
highest-ranked borrow: the audio might step where the words do not.
It is a change-point signal, not the rejected absolute-loudness work.

**Methodology.** Phase A measured the signal before building any
variant: feature series (frame RMS, spectral centroid, spectral
flatness) at 8 kHz mono; how often a spectral step sits within 1s of
a true edge, and how often a true edge has any qualifying pause
nearby. Frozen: `sb_eval/ab/splice_step/`.

**Results.** A spectral step sits within 1s of a true edge **1/34**
times on owner truth (0/18 on 286, 1/16 on 287), and on dynamic edges
— where the splice theory predicts it — **1/14**. Only 32% of true
edges have any ≥0.3s pause within 1s; strict MinusPod silences number
**zero** in both 286 and 287. Pooled across 8 episodes: 11/82
(13.4%), precision ~1% at ~73 events/hour. Megaphone mp3s do not
bracket splices with silence. No variant built; harness copy removed.

**Conclusion.** The signal is not there. Discard. The audio channel
does not step at the boundary on these feeds.

---

## 20. Per-feed jingle/stinger templates (MinusPod idea 2) — DEAD at Phase A (2026-11-11)

**Reason.** MinusPod captures a short recurring break cue once per
feed (MFCC fingerprint) and snaps a detected edge to it. A stinger at
the break would be a sub-second edge the transcript cannot see.

**Methodology.** Discovery scan on cached 286/287/1101/1103 audio:
edge-anchored recurrence of non-speech bursts. Frozen:
`tmp/jingle_snap/`.

**Results.** No recurring break stingers on either served show.
Edge-anchored recurrence means ~0.00–0.12; bursts at chance in all
four episodes scanned. The negative is real, not a sensitivity
failure — the same front-end scores the identical Progressive dynamic
ad at exactly 1.000 cross-episode. No variant built.

**Conclusion.** Dead for the served shows. The idea stays viable
only for a future show that actually plays a break stinger.

---

## 21. Cross-episode recurrence, NEGATIVE direction (MinusPod idea 3) — BORDERLINE, do not include (2026-11-11)

**Reason.** MinusPod uses 8-word shingles recurring across episodes
as a boilerplate hint. AdChisel's duplicate scanner already uses
8-word shingles within an episode; the negative direction would
suppress blocks in recurring intro/outro regions.

**Methodology.** A/B harness variant (`ab/xep_suppress/`), baseline
reproduced exactly first. Two findings recorded: the boilerplate-zone
restriction is load-bearing — recurrence alone would have suppressed
14/72 real matched blocks, because sponsor reads repeat
near-verbatim — and intro-zone suppression never fired.

**Results.** FP 36→34 (two genuine Huberman outro URL plugs
removed), everything else flat — recall 0.673, median 4.7s, mean
12.0s, within-1s 26.4% (38/144), per-edge deltas 0/0/144. Fails the
KEEP letter (median must improve).

**Conclusion.** Borderline/mixed → do not include. The two genuine FP
removals do not justify the residual risk (a fixed-script dynamic
pre-roll recurring in ≥2 priors could get suppressed with a larger
pool). 286/287/1101 checks were offline only; re-verify live if this
is ever revisited.

---

## 22. Cross-episode recurrence, POSITIVE direction (MinusPod idea 3) — KEPT (2026-11-11)

**Reason.** Same mechanism, inverted: sponsor copy recurring across
episodes corroborates a weak ad — targeting the tail-only-read
failure, where the tail contains sponsor copy repeated from other
episodes.

**Methodology.** A/B harness variant (`ab/xep_corroborate/`),
baseline reproduced exactly first. Post-processes blocks only; no
`find_mentions` calls, so no orphan-flip risk (checked clean).

**Results.** Recall 0.673→**0.701** (+3 Huberman labels, 0 lost), FP
36→**33**, median 4.7→**4.0s**, mean 12.0→11.9s, within-1s 26.4%→
**29.3%** (38→44 edges), within-2s 37.5%→40.0%. Common set: 14
better / 5 worse / 125 unchanged, within-1s 38→39. Honest caveat:
the median gain is compositional — common-set median is flat 4.7s —
but the composition change is *gaining* good matches, not losing bad
ones, and the rule passes on its letter. One large harm: LMNT on
Modern Wisdom Sydney Sweeney, start −0.8→−20.7s. Safety checks all
clean: 286/287 byte-identical, SN 1101 still 6 blocks, SN 1103
Hoxhunt unchanged. Live pipeline needs prior-episode transcripts as
input; a show with no priors is a clean no-op.

**Conclusion.** KEEP. Not yet merged — left in the harness copy only
(`ab_detect_xep_corroborate.py`); merge into `run_pipeline.py` waits
until the dynamic-slot validation driver finishes, so the slot
validation stays pure. Untested follow-up: a span-start-vs-first-
mention guard for the Sydney Sweeney harm.

---

## 23. DAI transition pairs (MinusPod idea 4) — DISCARDED (2026-11-11)

**Reason.** MinusPod's transition detector: paired ≥12 dB loudness
jumps, up then down, into 15–180s regions, promoted only with
transcript corroboration. Lowest-ranked candidate because it is the
loudness family (entry 3 rejected absolute loudness); the material
difference is paired jumps plus required corroboration, never
standalone.

**Methodology.** A/B harness variant (`ab/dai_transition/`),
baseline reproduced exactly first.

**Results.** SB headlines pass the decision rule's letter (recall
0.682, median 4.0s, within-1s 28.1%, FP flat 36; per-edge 2 better /
0 worse / 142 unchanged), but 286 is disqualifying: coverage
unchanged yet **over-mark 18→348s, false blocks 0→1** — a new 330s
Patreon false block (4543–4873), the known "patron saint" trap
promoted from suspect to marked chapter. The orphan-only
corroboration that wins SN 1096 is the same gate that promotes the
Patreon block. 287 unchanged; SN 1101 6 blocks identical.

**Conclusion.** Discard. The gate that helps on one show promotes a
house plug to a marked chapter on the served show. Untested
follow-up: strong-methods-only corroboration.

---

## 24. Silence-midpoint snap (MinusPod idea 5) — DISCARDED at quick check (2026-11-11)

**Reason.** MinusPod moves an edge to the midpoint of the nearest
qualifying silence span, capped at 2s. Re-test of a rejected family
(silence+sentence snap, all three forms, entry 7); the anchor differs
materially (silence midpoint, not sentence end).

**Methodology.** Offline simulation on frozen blocks, two rules,
before building a variant. Frozen: `ab/silence_midpoint/` (+ `_b`).

**Results.** Within-1s 26.4→25.0% (rule A) and 23.6% (rule B);
per-edge 14 better / 23 worse, harm concentrated in ends; on owner
truth only 1/34 edges moved at all. Same failure shape as the
earlier snap tests.

**Conclusion.** Discard. The refined edge is already within ~1s and
the nearest silence is 1–2s away.

---

## 25. Publisher-transcript differential implementation — IMPLEMENTED, ready to merge (2026-11-11)

**Reason.** MinusPod's transcript-differential: if the feed carries a
`podcast:transcript` tag whose transcript was made before ads were
spliced in, Whisper-only runs (≥40 words, ≥10s) are candidate ads.
The owner directed implementation (many podcasts carry publisher
transcripts, just not the two served shows). The Omny test the same
day (Cabinet of Curiosities 873/874/875) found exactly 3 ads per
episode, 9 total, all cut as clips for a listening check.

**Methodology.** Scratch module `tmp/transcript_diff/
transcript_diff.py` (661 lines, stdlib only, 26 synthetic checks
passing; one real bug found and fixed — SRT timecode parser missing
`* 60` on minutes). Deliberate design call: **corroboration only, no
heat-map vote** — a vote would be a marking-behavior change needing
an A/B run. No `find_mentions` calls, so no orphan-flip risk.
`run_pipeline.py` untouched (0 refs). Merge is mechanical per
`tmp/transcript_diff/INTEGRATION.md` §1–§8, stage running after the
dynamic-slot flow and before chapter building.

**Smoke tests (real feeds).** Podcasting 2.0 ep 235 (ad-free) — 0
gaps, coverage 0.919. Cabinet of Curiosities "Wild at Heart" — **2
gaps, both verified BetterHelp ads** (77.9s and 62.9s), VTT and SRT
identical. Graceful degradation verified both ways (no tag →
skipped; transcript-includes-ads → 0 gaps).

**Listening check (owner, 2026-11-11, Cabinet of Curiosities
873/874/875, 9 clips).** Verdict: a smashing success. 875: ad 1 a
few ms short at the end, ad 2 perfect, ad 3 missing a word or two at
the start. 874: all three perfect. 873: all three ~0.25s short at
the end. Edges are already inside the 1–2s goal; the ~0.25s
short-at-end pattern on 873 is a possible future refinement (a small
end pad), not a defect to fix now. Owner's observation: the mid-roll
on all three episodes is a host-read ad — the publisher transcript
omits host-read reads too, not just dynamic insertions — so the
differential may be a general ad detector for transcript-carrying
shows. That hypothesis is untested beyond this show.

**Conclusion.** Implemented and ready to merge. Merge waits until
the dynamic-slot validation driver finishes, so the slot validation
stays pure — same hold as entry 22.

---

## 26. Publisher-transcript differential on 3 more shows — HYPOTHESIS NOT CONFIRMED beyond Omny (2026-11-11)

**Reason.** The owner's hypothesis from the Omny listening check:
publishers generally omit ad reads — including host-read ads — from
their transcripts, so the differential would be a general ad
detector for transcript-carrying shows. It rested on one show. Test
it on 2–3 more transcript-carrying shows.

**Methodology.** Discovery first: 20 candidate feeds checked against
raw feed XML for `podcast:transcript` tags. Most major publishers
carry none (Planet Money, 99% Invisible, Radiolab, This American
Life, The Daily, SmartLess, Front Burner, Today in Focus, Tim
Ferriss, The Rest Is Politics, and more), even though they publish
transcripts on their websites. Only four confirmed with tags: No
Agenda (250), Podcasting 2.0 (210), The Bootstrapped Founder (870,
Transistor), Contracting Officer Podcast (76, not selected — almost
certainly no ads). Three shows selected, 2 episodes each, most
recent with tags. Same method as the Omny test: pipeline
`transcribe_chunked` (base), diff vs publisher transcript, ≥40
words / ≥10s gaps are candidate ads, cut as clips. Work under
`tmp/transcript_diff_more/`.

**Results — 0 of 3 shows could test the hypothesis; no candidate ads
anywhere, no clips cut.**
- Podcasting 2.0 (nashownotes SRT), Ep 277: 0 gaps, coverage
  0.8915. Ep 276: ONE gap, 17.1–49.4s, 88 words — a FALSE POSITIVE:
  show-intro patter; the publisher SRT has a hole in the intro
  region (block 4 spans 16.6–40.2s with ~10 words). No brand, URL,
  or code. Both episodes genuinely have no ads (value-for-value
  show).
- No Agenda (nashownotes SRT), Eps 1916/1915: NOT transcribed. The
  saved publisher SRTs were scanned first and CONTAIN the
  promotional reads (Giggle Coffee Roasters, "visit
  CoffeeRoasters.com, use code ITM20 for 20% off your first
  order", 1916 03:18:28; personofmeritcomics.com; Sklazen.com;
  similar plugs in 1915), so the differential is vacuous by
  construction — transcribing ~9.6h of audio to confirm 0 gaps
  would add nothing about the hypothesis. The plugs are
  donor-business plugs, not paid sponsor reads; either way the show
  cannot test the hypothesis.
- The Bootstrapped Founder (Transistor VTT), Eps 439/438: 0 gaps,
  coverages 0.9647/0.9654. No paid sponsor ads; the PodScan
  self-plug is in the publisher VTT too.

**Conclusion.** The hypothesis still rests on Cabinet of Curiosities
alone. What the round established: (1) the differential's
negative-control behavior is clean — 0 gaps on 4 ad-free episodes;
(2) nashownotes transcripts are full transcripts of the final audio,
promotional segments included; (3) a gap is not necessarily an ad —
a publisher transcript can have holes, which validates the
corroboration-only design of entry 25. The discriminator is HOW the
transcript was made (pre-ad vs final-audio), not the publisher. A
future test needs shows whose transcripts are made pre-ad AND that
carry paid sponsor reads.

---

## 27. Phonetic vote inside blocks — DISCARD both gates (2026-11-12)

**Reason.** The phonetic redesign (entry 14) failed because its hits
flipped sponsors from the wide orphan path to the narrow brand path.
The redesign's own record suggested the remaining use: phonetic hits
only as a vote inside already-detected blocks, never creating
segments. The owner approved testing it.

**Methodology.** Harness only; baseline reproduced exactly first
(combined_repro18, all deltas 0.0). Hit finder reused from the
redesign (Soundex + LCS, |length diff| ≤ 2). Post-processing on
detected blocks: a phonetic match of the block's own sponsor within
60s outside an edge extends that edge to the hit. Two gates: A
(both edges, any hit), B (end edges only, hit must have a
phonetically-matching URL stem nearby). Offline simulation first,
then full 25-episode runs.

**Results vs baseline (recall 0.673, median 4.7s, mean 12.0s,
within-1s 26.4%, FP 36, missed 35):**
- A: recall 0.673→0.682 (72→73 matched, missed 35→34, FP 36→35),
  median flat 4.7s, mean 12.0→12.3s, within-1s 26.4%→26.0% (38
  edges, count flat), within-2s 37.5%→37.0%. Per-edge: 1 better /
  3 worse / 140 same. The 1 better edge is the SN 1099 Bitwarden
  end (29.9→21.2s). The 3 worse are content collisions pushing
  already-early starts further out — Modern Wisdom Sydney Sweeney
  Whoop start 8.3→10.4s, Willpower Momentous 7.3→10.8s, SN 1096
  Material 26.0→74.5s ('mature' ~ 'material', 48.5s before the
  block). The "gained" label is a reclassification, not a new
  detection (a baseline FP block extended to 62% overlap with a
  −22.1s end error). 6 firings total.
- B: every headline identical to baseline; 1 better / 0 worse /
  143 same (the same SN 1099 Bitwarden end). 2 firings.
- SN 1103 Hoxhunt (the motivating case): block identical in
  baseline and both variants (440–616.4s) — the 'hawkshunt' hits
  sit inside the block already, so there is nothing outside to
  extend toward.

**Conclusion.** DISCARD both — the decision rule fails on median
(flat in both) and on within-1s (down in A). B's single better edge
is the same one the closing-URL fallback (entry 3) already fixes
better (−29.9→−8.5s), and that fallback was discarded as
unmeasurable on 25 episodes. More data would not resolve this: the
firing rate (6 in 25 episodes) is too low for the median to move,
and the harm mechanism (a phonetic collision just outside an early
edge) is systematic. The phonetic idea in this form is closed:
hits are real evidence of a URL tail, but there is nowhere useful
left for them to vote — the orphan merge already extends blocks to
the tail hits that matter, and the remaining outside hits are
collisions. Variant code removed entirely; `ab/phonetic_vote.py`
kept as the test record; `run_pipeline.py` never touched.

## 28. Synthetic ground-truth episode, spliced (2026-11-12)

**Reason it was a candidate.** The owner has no time to manually mark
more episodes, so verified labels must come from elsewhere. A synthetic
episode splices already-verified ads into an ad-free value-for-value
show: the truth is the splice map itself, exact to the millisecond,
and the detector runs end to end on real audio.

**Methodology.** Base: Podcasting 2.0 Ep 277 (5494.5s, confirmed ad-free
value-for-value). Its donation appeal was cut at 4553.92–4825.68 (base
clock) by keyword scan; the remaining text was verified free of
donor-read language; cleaned base 5224.537s. 27 inserts: all 26 verified
ads (9 from Unsubscribe 286, 8 from 287, 9 listening-verified Omny
Cabinet of Curiosities clips) plus the Pepperbox house plug (287,
6055–6085) as a 27th insert; the Factor segment was NOT included (owner
ruled it not an ad). Clips cut at speech boundaries via word timestamps;
assembled with ffmpeg hard cuts, no crossfades; truth from
ffprobe-measured durations (6927.957s, 52 pieces). Placement: pre-roll
(286 Progressive), several mid-rolls, post-roll (286 Perfect Jean),
one back-to-back pair with zero gap (Stop Box + SuperPower). The
description was synthesized in real-description style and verified with
the pipeline's own `extract_sponsors`: all 16 sponsors extract,
Pepperbox not a sponsor. (One deviation: Stop Box's domain written as
stopboxusa.com — stopbox.com contains the substring "x.com" and is
dropped by the pipeline's social-platform skip list.) The detector ran
unmodified (`transcribe_chunked` base + `detect_ad_blocks`) on the
synthetic episode and, as a false-positive control, on a null episode
(cleaned base, no inserts). Artifacts: `tmp/synthetic/` (episode,
null episode, `truth.json`, `description.txt`, `score_synthetic.json`,
`NOTES.md`, detect JSONs).

**Results (verified against `score_synthetic.json`).** Recall at ≥50%
22/27 (0.815); at ≥80% 18/27 (0.667). 21 of 26 ads caught with a block;
19 with the sponsor named correctly — the Ridge block is labeled
"rich" (a mishearing of Ridge) and the 874 Coca-Cola block is labeled
"Aura" (wrong sponsor, see below). Median edge error 2.55s, mean 7.95s,
within 1s 8/44 (18.2%), within 2s 16/44 (36.4%). Ends beat starts: 6
of the 8 within-1s edges are end edges; starts skew early (worst
−60.2s). Pepperbox correctly labeled as a house plug (block overruns
its end by +29.2s). False blocks: 3 (a content-URL block, a 2.2s
refinement stump, the Perfect Jean fragment). Null episode: **8 false
blocks in 87 minutes**, mostly reverb pairing with content URLs or dry
remote-host audio. All 6 BetterHelp ads caught; Unsubscribe ads 14/17
at ≥50%.

**The 5 misses, verified reasons.** (1) 875 Coca-Cola (28.4s):
under-floor — only the duplicate segment covers it, evidence span
~10s. (2) 873 opening (41.0s): only reverb + voice fire, overlap 20s
< 30s floor; the three "Coca-Cola" mentions produce no brand segment.
(3) Leaf Filter 286 (30.7s): refinement collapse — a heat-map block
did form, but refinement moved its end 5713 → 5642, leaving a 2.2s
stump, because the block was labeled by a content URL (indexrobot) and
refinement anchors on the block label's own mentions. (4) Stop Box
(50.7s): back-to-back fusion — Stop Box + SuperPower fused into one
heat-map block (reverb island and voice bridge the boundary, the
SuperPower orphan window reaches back inside Stop Box); cap +
refinement kept only the SuperPower half. The detector has no
mechanism to split a fused block. (5) Perfect Jean (104.4s,
post-roll): the cluster segment was dropped as an unanchored
single-cluster block because the spoken URL falls 5s outside it; the
surviving block covers 38%.

**Root cause behind 4 of the 5 misses — brand-orphan MAX_AD_LEN gate.**
`_method_brand_orphan` computes one segment per sponsor spanning its
first-to-last mention across the whole episode (±45s padding) and drops
it when the span exceeds `MAX_AD_LEN` (720s). Coca-Cola (1145s span),
Leaf Filter (~3270s), Perfect Jean (stray content mentions), and Stop
Box (stray substring hits) all exceed it and are silently dropped. This
is the same silent-drop failure mode the orphan method was created to
prevent (2026-10-14), recurring because the orphan is per sponsor per
episode, not per ad. **Also found:** substring false matches in
`find_mentions` (short-variant path, `if v in w`, no guard) — "aura"
inside "restaurant" (13 mentions) produced the Aura label, and "stop"
inside "Christopher"/"stopping" produced phantom Stop Box mentions.
**Also found:** reverb fires almost everywhere on this episode (median
lateAC 0.306 ≥ 0.25 gate) — a near-constant background vote that pairs
with any other weak method, behind most null-episode false blocks; and
refinement can shrink blocks below the floors (2.2s, 11s, 20.1s
survivors exist).

**Conclusion.** The synthetic set works as a test bed: it found two
real, fixable defects (orphan episode-wide span; substring leak) and
confirms the honest reading that spliced ads are easier than real
host-reads while the base content's URL/brand density is real. No
detector tuning was done. Next-step candidate (not started, owner
decision): split the orphan into per-mention-cluster segments instead
of one episode-wide span.

## 29. In-house TTS pilot episode (2026-11-12)

**Reason it was a candidate.** Spliced ads are a different voice in a
different room, so they are easier than real host-read ads. An
AI-generated episode puts the ad in the SAME voice as the show
content, replicating the hard host-read case, with exact script truth.

**Methodology.** Engine: **Piper 1.8.0** (not Kokoro). Kokoro was tried
first: attempt 1 failed when pip's temp dir (/tmp, 512 MB tmpfs) filled
on the 539 MB torch wheel; attempt 2 was a multi-GB CUDA torch stack
at ~2 MB/s on a CPU-only VM, stopped per the fallback condition.
Piper installed in under a minute, no torch. Voices:
`en_US-lessac-medium` (host) and `en_US-amy-medium` (produced ad).
Proof before building: a 5s sample transcribed by the pipeline's own
faster-whisper base at word agreement 1.000. Episode 5:06, 1,006
words, a library summer-reading-program explainer; 3 ads: Ghostbed
host-read, same voice, 37.34–94.54 (57.2s); Manscaped host-read, same
voice, 155.22–204.38 (49.2s); Leaf Filter produced-style, different
voice, 249.44–279.87 (30.4s). Description synthesized and verified —
all 3 sponsors extract via `extract_sponsors`. Detector run
unmodified, scored vs `truth.json`. Artifacts: `tmp/tts_pilot/`
(episode, `truth.json`, `script.txt`, `description.txt`, `detect.json`,
`score.json`, `NOTES.md`).

**Results (verified against `score.json`).** Recall 2/3 (0.667) at both
≥50% and ≥80%; **0 false blocks**; 4 of 6 edges matched, within 1s
1/4, within 2s 2/4, median edge error 2.66s, mean 2.44s, max 5.0s.
Ghostbed caught at 86.6% (+5.0/−2.66 — the ad opens with 5s of
brand-free lead-in copy and refinement pulled the start to the first
brand evidence). Leaf Filter caught at 93.1% (+1.18/−0.91, the
best-edged block). **Manscaped missed — a transcription defect, not a
detection defect:** of 3 spoken "Manscaped" instances only 1 came back
exact; both ".com" reads came back "manscape" (the known base-model
defect, reproducing even on clean TTS audio), and the promo code
"SUMMERREAD" came back "Summaryd" twice, killing the CTA evidence; the
remaining signals stayed single votes and never promoted. Voice found
0 anomalous segments (including on the different-voice ad). Reverb
stayed disabled (episode median lateAC 0.100 — dry TTS audio, gate
worked as designed). Silences found: 0. The ads sit only ~45s apart,
so the wide orphan segments bridged Manscaped and Leaf Filter into one
heat-map block (149–286s) before cap/refinement split it.

**Conclusion.** The route is proven: Piper works on this VM, script
truth is exact, the detector runs end to end. Same-voice host-read ads
ARE detectable — Ghostbed was caught with all four text methods
voting. The pilot's limit, stated honestly: TTS audio has no room
tone, ums, or crosstalk, so it tests edge accuracy and detection
logic, not ASR robustness. Recommended next step (not started): scale
to a 60–90 minute multi-speaker episode with disfluencies written into
the script, a room-tone bed mixed under the TTS, and 6–8 same-voice
host-read ads; a Kokoro revisit (better prosody) after that.

---

## Earlier rejected ideas — one-line index

These predate the test log and are covered in the README sections
named; they are listed here so this file is the complete index.

- **Loudness/compression** (2026-10-28, REJECTED) — produced spots
  are not louder or more compressed than content on these feeds; see
  README "Loudness/compression".
- **Ollama / LLM reviewer as detector** (2026-10-14, REJECTED) —
  hallucinated sponsor reads, missed real ads, garbled timestamps,
  too slow and memory-hungry; see README "Ollama / LLM reviewer".
- **Narrative-break / semantic topic change** (tested twice
  2026-10-28, REJECTED, not wired in) — TF-IDF fired 141/163
  segments; real embeddings still gave 57 segments per episode with
  27–33 false segments; conversational topic drift triggers "islands"
  everywhere; see README "Detection architecture", `detect_narrative_breaks`
  note.
- **Greedy decoding as transcription replacement** (2026-11-06,
  REJECTED) — slower than beam search with lower word agreement;
  recorded in the local HANDOFF.md, not in the README.
- **whisper.cpp as transcription replacement** (2026-11-06, REJECTED)
  — slower on this CPU; recorded in the local HANDOFF.md, not in the
  README.
- **Tiny model as transcription replacement** (2026-11-06, REJECTED)
  — real accuracy loss vs base; recorded in the local HANDOFF.md,
  not in the README.
- **Shrink-to-core edge mode** (2026-10-29, REJECTED by the owner the
  same day) — collapsed sparse-mention reads (Club TWiT 75s → 2s);
  replaced by the middle-ground edge refinement; see README
  "Middle-ground boundary refinement".
- **Silence-based start fallback** (2026-11-07, REJECTED) — would
  extend the Eight Sleep block 73s backward into the preceding Joovv
  read; see README "SponsorBlock eval A/B" and entry 2 above.
