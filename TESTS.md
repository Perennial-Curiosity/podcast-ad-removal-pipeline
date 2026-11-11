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
