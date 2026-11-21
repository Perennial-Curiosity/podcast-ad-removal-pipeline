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

## 30. Brand-orphan split into per-mention clusters — DISCARD at all three gaps (2026-11-12)

**Reason it was a candidate.** The synthetic-episode test (entry 28)
showed 4 of its 5 misses trace to one defect: `_method_brand_orphan`
builds one segment per sponsor spanning first-to-last mention across
the whole episode and silently drops it when the span exceeds
`MAX_AD_LEN` (720s). Splitting mentions into clusters — one orphan
segment per cluster — should recover multi-ad sponsors without the
episode-wide drop.

**Methodology.** Gap derived from data, not guessed: 1,372
same-sponsor gaps on the 25-episode SponsorBlock set (median 10.7s,
71.6% under 30s, then a flat tail with no sharp valley — dense mentions
inside a read, sparse mentions across ads); the synthetic episode's 128
gaps show a clean valley at 90–180s. Gaps 60/120/180s tested; 120s is
the distribution-supported gap and also guarantees no same-sponsor
cluster overlap (max padding + snap 102s < 120s), so split segments can
never double-vote the heat map. Baseline reproduced exactly first
(`combined_repro19`: recall 0.673, median 4.7s, mean 12.0s, within-1s
26.4%, FP 36, missed 35). Frozen records: `ab/orphan_split60/120/180/`,
`orphan_split/` (120, canonical), `combined_repro19/`,
`orphan_split_gap.json`, `orphan_split_deltas.txt`.

**Results (verified against the frozen `results.json` files).**

| Gap | Recall | Matched | Missed | FP | Median | Mean | Within-1s | Within-2s | Max |
|-----|--------|---------|--------|----|--------|------|-----------|-----------|-----|
| baseline | 0.673 | 72 | 35 | 36 | 4.7s | 12.0s | 26.4% | 37.5% | 79.3s |
| 60 | 0.710 | 76 | 31 | 63 | 4.9s | 14.1s | 25.0% | 35.5% | 120.7s |
| 120 | 0.729 | 78 | 29 | 60 | 4.9s | 17.8s | 24.4% | 35.3% | 529.9s |
| 180 | 0.738 | 79 | 28 | 55 | 5.6s | 15.2s | 24.1% | 34.8% | 269.4s |

The recall gain (+4 to +7 labels) is real, but every gap fails the
decision rule on three of its four conditions: median worse, within-1s
down, FP up +53% to +75% (allowance ~10%). Common-set per-edge deltas
are net worse at every gap (120s: 4 better / 7 worse / 131 same of
142), and the gained labels carry bad edges — the 529.9s max error at
gap 120 is a 552-second "Civil War History" block matched against a
19-second label.

**Why it fails — the mechanism, verified in the logs.** The old
MAX_AD_LEN drop was load-bearing as a noise suppressor. Sponsors whose
mentions span the whole episode are usually being discussed as content
(guest names, ordinary words, the show's own topic), not running 3
ads. Splitting emits orphan segments for every content-discussion
sponsor — "David" blocks all over Huberman motivation, "material"
blocks on SN 1099, "RIDGE" blocks on 287 — and each pairs with
reverb/cluster/voice to form marked blocks. The trade-off exists at
every gap; more data would not resolve it (systematic).

**Secondary checks (all at gap 120).** 286/287: 17/17 at ≥50% holds
(9/9 and 8/8; caught80 7/9 and 6/8 unchanged), but over-mark explodes
— 286: 17.8→76.2s, false 0→1 (a Patreon block); 287: 41.2→300.8s,
false 1→4 (three new RIDGE blocks). SN 1101: 6 blocks in both runs,
identical spans and labels. SN 1103 Hoxhunt: block identical in
baseline and variant (440–616.4s); the orphan method emits nothing on
1103 in either run — `find_mentions` finds zero Hoxhunt mentions (the
transcript says "hawkshunt") — so the orphan-flip constraint is
satisfied trivially and the block stays caught. Synthetic episode:
caught50 22→24/27, caught80 18→21/27; recovered one Coca-Cola (≥50%),
Stop Box and Perfect Jean (≥80%); still missed one Coca-Cola and Leaf
Filter; **two regressions** (Ridge 1.00→0.00 — a stray "stop" substring
hit stole the block's label and capped it to the Stop Box span;
SuperPower 0.98→0.00 — the Stop Box + SuperPower fusion split kept
only the Stop Box half). FP 3→5; median 2.55→3.56s. Null episode
identical (8 blocks); TTS pilot identical (2/3, 0 FP). **Substring
pollution (recorded, not fixed here):** Stop Box's five singleton stray
"stop" hits each became a 90-second orphan segment; two caused real
harm (the Ridge theft and a 593.1–653.1s false block). The split
amplifies the substring leak: previously a stray hit only stretched the
episode-wide span toward a drop; now each stray hit becomes its own
voting segment.

**Conclusion: DISCARD at all three gaps.** Variant code removed
entirely from the harness; `run_pipeline.py` never touched; no patch
written (nothing to apply). A possible future direction is noted, not
attempted: gate orphan segments on corroboration — a cluster only votes
if it has ≥2 mentions or ad evidence (CTA/URL/duplicate) nearby — to
separate "sponsor in 3 ads" from "sponsor discussed as content."

## 31. Substring-leak fix in find_mentions (word-boundary guard) — KEEP the single-letter guard (2026-11-12)

**Reason it was a candidate.** `find_mentions` matched short brand
variants (<6 chars) as a bare substring anywhere inside a transcript
word (`if v in w`, line 669). Verified consequences: "aura" inside
"restaurant" (13 mentions) produced a cluster-popular "Aura" segment
and a wrongly labeled "Aura" block on the synthetic episode's Coca-Cola
ad; "stop" inside "Christopher"/"stopping" produced phantom "Stop
Box" mentions (the missed-sponsor check counts 7, only 2 real). Same
leak class as the 287 Factor bug the code already comments on.

**Methodology.** Three guard forms tested in the harness; baseline
reproduced exactly first (`combined_repro20`: recall 0.673, median
4.7s, mean 12.0s, within-1s 26.4%, FP 36, missed 35). The guard
requires a word boundary on both sides: **before** — start of token or
a non-alphanumeric char; **after** — end of token, a non-alphanumeric
char, a plural/possessive suffix, or a **single-letter remainder** (the
token is the variant plus one letter — a spelling/transcription variant
of the brand, the short-variant analogue of the len≥6 fuzzy path's
≥0.85 ratio). A multi-letter remainder (`sleeping`, `seeding`) still
fails. The len≥6 exact/fuzzy path, split-compound branch, and
multi-word path are untouched. The **simple** guard (boundary only)
and the **prefix** guard (+ marketing-prefix allowance for
`drinkag1`/`drinklmnt`/`takeultra`) were also tested; the prefix form
was identical to the simple guard on every measurement — its 22
recovered hits are redundant because the full variants (`drinkag1`,
`drinklmnt`, len≥6) already match those tokens exactly.

**Removed-vs-kept hits (25-episode set).** Short-path hits **698 →
481 (217 removed, 31.1%), all phantom**: `aura` 10→0 (all `restaurant`
forms), `mitt` 48→0 (`committee`, `committed`, `permitted`,
`transmitting`, `intermittent`, `neurotransmitter` — zero real hits),
`ridge` 15→9 (removed 6: `cartridge(s)` + `bridger`, all content
words), `ag1` 103→85 (removed 18, all `drinkag1`), `sleep` 170→121,
`lmnt` 2→0 (both `drinklmnt`). Kept: exact 360, plural/possessive 20,
URL/hyphen/punctuation 101. The single-letter allowance re-admits only
4 hits beyond the simple guard (`sleepy` ×2, `placed` ×2) — and
recovers `noble` ×3 on 286 (see below). Self-test: 37/37 boundary
cases pass; full-function diff vs the original removes 14 hits, adds 0.

**Results (verified against the frozen `results.json` files).** All
three guard forms are identical on the SponsorBlock set:

| | Recall | Matched | Missed | FP | Median | Mean | Within-1s | Within-2s | Max |
|---|---|---|---|---|---|---|---|---|---|
| Baseline | 0.673 | 72 | 35 | 36 | 4.7s | 12.0s | 26.4% (38/144) | 37.5% | 79.3s |
| Guard | **0.682** | **73** | **34** | **35** | 4.7s | 12.4s | **26.7%** (39/146) | 37.7% | 84.9s |

Common-set per-edge deltas: **0 better / 0 worse / 144 same**. 1 label
gained (Huberman Rorra 926–1097.6 — phantom `sleep` hits in the ad's
tail had voted for Helix Sleep, leaving the Rorra URL evidence a
single-vote suspect), 0 lost. The mean/max worsening is a composition
artifact of the gained label's +24s start edge.

**Secondary checks.** 286/287: the simple and prefix guards **lose
the NOBL ad on 286** (9/9 → 8/9, coverage 606.7 → 547.6s) — Whisper
renders NOBL as `noble` (3×)/`nobles` (1×). The **single guard
recovers it: 286 9/9, 7/9 at ≥80%, coverage 606.7/673s, over-mark
17.8s, false 0 — identical to baseline**. 287 identical under all
forms (8/8, 6/8, 463.0/531s, false 1 = Pepperbox). **17/17 at ≥50%
holds.** SN 1101: 6 blocks, identical spans and labels under all four
implementations. SN 1103 Hoxhunt: block identical 440–616.4s (labeled
`hawkshunt`) under all forms; all 6 blocks identical. Orphan-flip
constraint satisfied. Synthetic episode (single guard): caught50/80
**22/27 + 18/27 unchanged**, FP 3, median 2.55s, mean **7.95 →
6.82s**. The **"Aura" mislabel on the 874 Coca-Cola ad is gone**
(baseline block 1020.0–1104.8 labeled `Aura`, start error −55.5s →
single block 1066–1106 labeled `dry-island`, start error −9.6s; the
one within-1s loss, 8→7 edges, is that block's end edge 0.8 → 2.0s).
Simple guard loses NOBL here too (cov 0.74 → 0.00, plus a `nobletravel`
FP fragment). Ridge label unchanged (`rich` — a Whisper mishearing of
the ridge.com URL, not a leak artifact). Stop Box fusion unchanged
(still missed). Null episode: 8 blocks under all forms, but the
**"Aura" block (800–830s) is gone** under all three guards (replaced by
a reverb/voice `dry-island` block 760–831s). TTS pilot: identical 2/3,
0 FP under all forms. Missed-sponsor counts (synthetic): Stop Box **7
→ 4** (2 real + 2 genuine standalone `stop` in PC20 content; removed:
`stopping` ×2, `Christopher` ×1), Aura **14 → 0** (13 `restaurant`
forms + `laura`). On 287 itself: 2 → 2 (no phantoms there).

**Decision-rule status — stated plainly.** The variant **fails the
letter of the decision rule on the median (4.7 → 4.7s, flat)** —
because zero existing edges move (0/0/144 on the common set), the
median structurally cannot move. Every other condition passes
(within-1s up, recall up, FP down), and the fix removes
provably-false evidence with **zero regressions on any check**. It is
kept as a **correctness fix**, not a detection improvement, and the
median caveat is recorded here and in the module docstring. More data
would not change the median (the fix's SB-edge effect is structurally
zero). The simple guard is discarded (loses NOBL); the prefix variant
is discarded (redundant). Loser wiring was removed from
`ab_detect.py`; `ab/substring_fix.py` holds the final guard only, and a
post-cleanup 1103 re-run reproduces the frozen blocks exactly.

**Out of scope:** the multi-word path in the same function
(`all(toks[j] in nw[i+j]...)`, ~line 674) uses the same per-token `in`
pattern but requires consecutive tokens — far smaller leak surface;
deliberately left alone. (The Stop Box phantoms came through the short
path — its variants include the single token `stop` — so the patch
covers the measured defects.)

**Patch (merge held — do NOT apply yet).** `tmp/substring_fix_patch.diff`
adds `_boundary_match()` and changes only the short-variant branch of
`find_mentions` (+1 docstring line). Verified: `patch --dry-run` clean,
patched copy compiles, its `find_mentions` is identical to the tested
harness variant, 30/30 boundary cases pass. `run_pipeline.py` was
never touched (the original `if v in w:` is still at line 669). The
merge waits for the dynamic-slot validation driver to finish, like the
recurrence, transcript-differential, and orphan-split merges. Frozen
records: `ab/combined_repro20/`, `ab/substring_fix_single/` (kept),
`ab/substring_fix.py`, `ab/analyze_substring_fix.py`,
`ab/check_substring_fix_286_287_1101.py` + `.log`,
`ab/check_substring_fix_synth_tts.py` + `.log`.

---

## 32. Omny transcript-differential end-pad — ADOPT +0.50s on the trimmed boundary; raw boundary needs trimming, not padding (2026-11-13)

**Reason:** the owner's listening verdict on the Omny clips (2026-11-11) was a
"smashing success", but he noted 873's clips were ~0.25s short at the end.
The question was whether a small end-pad improves the differential's end
boundaries.

**Method:** On Cabinet of Curiosities 873/874/875 (9 ads), transcribe 1s
of audio immediately after each end boundary with faster-whisper base and
classify whether ad content continues or the show resumes, cross-checked
against cached full-episode word timings. Two boundaries were probed
separately: the differential's raw end boundary (the module's find_gaps
"end") and the clip end boundary (floor of the true ad end — the last ad
word's end time rounded down to the whole second, which is what the owner
heard). True ad end = end of the last ad-copy word from cached word
timings.

**Results:** The two boundaries are different things. Clip boundary: mean
error −0.46s, all 9 inside 1s — the owner's "~0.25s short" confirmed and
quantified (on 873's clips: 0.02 / 0.68 / 0.90s short). Raw diff boundary:
mean error +13.7s, only 1 of 9 inside 1s — it over-extends because the
publisher transcript also omits adjacent non-ad material (show intro
+9–11s on all three openers, outro bumper +4s, post-midroll silence plus
unaligned show-resumption words +40s on both 874 and 875 mid-rolls).
Probe A (1s after the raw diff end): ad content continues on 0 of 9 (5
show resumes, 4 silence/end-of-file). Probe B (1s after the clip end):
ad content clearly continues 0.40–0.90s on 6 of 9, and some ad audio
extends past the boundary on all 9. Pad candidates on the trimmed
boundary — no pad: mean |error| 0.46s; +0.25s: mean 0.32s, max 0.65s,
3/9 exact; +0.50s: mean 0.28s, max 0.48s, 5/9 exact. No pad pushes any
end edge into story content; tightest case is 875 ad3, where the network
bumper starts 0.56s past the clip end and the +0.50s pad stops 0.06s
short of it. Isolated 1s probe transcriptions hallucinate on
silence/near-EOF audio; classifications rest on the cached full-episode
word timings. Probe runs were slow (~3 min per 1s clip) under CPU
contention from the validation driver and the TTS worker. Two tooling
failures worked around, not skipped: faster-whisper's Hugging Face hub
attempt choked on a malformed proxy env var (fixed by passing the local
snapshot path), then PyAV decode failed (fixed by decoding to raw PCM
with ffmpeg and passing a numpy array).

**Conclusion:** ADOPT a +0.50s end-pad, applied ONLY to the end boundary
AFTER it is trimmed to the last ad word — never to the raw differential
boundary. The larger finding: the differential module's raw end boundary
over-extends by a mean of 13.7s, so the pad only makes sense as part of a
trim-to-last-ad-word step; without trimming, a pad would be harmful. The
"smashing success" verdict was about the trimmed clips, not the raw
differential boundaries. The trim + pad belong in the transcript-diff
module before its held merge. Records:
`tmp/omny_transcript_diff/end_pad_report.md`,
`tmp/omny_transcript_diff/probes/probe_results.json` (18 probes).

---

## 33. Omny transcript-differential start-pad — ADOPT NOTHING, trim-only (2026-11-13)

**Reason:** The end-pad test (entry 32) found the differential's raw end
boundary over-extends by a mean of 13.7s and adopted a +0.50s end-pad
only after trimming to the last ad word. The owner directed measuring
the start boundaries the same way before implementing the trim+pad.

**Method:** Same probe method as entry 32, on the same 9 Omny ads:
transcribe 1s of audio immediately BEFORE each start boundary with
faster-whisper base, classify ad vs show content against cached
full-episode word timings. Two boundaries per ad: the raw diff start
(find_gaps "start") and the clip start. True ad start = start of the
first ad-copy word. Clip start convention, determined from the data:
floor(true ad start) for the 6 mid-roll and outro ads; the 3 openers
start at 0.0 (file start).

**Results:** Clip boundary (copy-based): mean error −2.39s, 6/9 inside
1s (all except the three openers). Raw diff boundary: mean error
−7.61s, only 3/9 inside 1s — the three mid-rolls, where the diff start
IS the first ad word (errors 0.00 / −0.04 / +0.04s). Openers: the diff
start (0.0) sits 5.50–7.80s before the copy starts; before the copy
sits fixed opening material (iHeart bumper, "Guaranteed Human" tag, and
on 873 a cross-promo). Under a region-based reading (the whole opening
block counts as the ad), the clip mean error is 0.30s (9/9 inside 1s)
and the diff mean error is 5.52s (6/9 inside 1s). Outros: the diff
start lands inside the Patreon house plug, ~1.6s after the plug begins
(the plug's opening words are in the publisher transcript, which is why
the diff starts mid-plug); the 875 outro is extreme, 32.50s early, with
26.7s of silence between the sign-off and the read. Probe A (1s before
the raw diff start): ad content begins before it on 0 of 9 (3
house-plug, 3 silence, 3 no audio before file start). Probe B (1s
before the clip start): ad content begins before it on 0 of 9 (6
silence, 3 no audio). Pad candidates on the trimmed boundary (first
ad-copy word = true start, error 0 by definition) — none: mean 0.00s,
9/9 exact; −0.25s: mean 0.25s, 0 overshoots; −0.50s: mean 0.50s, 1
overshoot (873 ad1 eats 0.06s of the Iris Palmer promo tail). Both pads
make every start edge worse by exactly the pad size.

**Conclusion:** ADOPT NO start-pad — trim-only. No ad audio begins
before the trimmed boundary on any of the 9 ads, so there is nothing
for a negative pad to recover. The asymmetry with the end side is
structural: the clip end sits before the last word ends (floor), so the
word's tail extends past the boundary and a +0.50s pad recovers it; the
clip start sits before the first word starts (floor), so the audio
before the boundary is pre-ad silence or plug. The finding that matters
for the module is the trim: the raw diff start is exact on mid-rolls
but 5.50–7.80s early on openers and 8.28–32.50s early on outros — and
trimming to the first word of the gap run fixes only the mid-rolls; the
outros need the trim to find the first sponsor-copy word, not just the
first word of the gap. Together with entry 32, the module needs a
trim-to-first/last-ad-word step on both edges, plus the +0.50s end-pad,
before its boundaries can be trusted. Records:
`tmp/omny_transcript_diff/start_pad_report.md`,
`tmp/omny_transcript_diff/probes/start_probe_results.json` (18 probes),
`tmp/omny_transcript_diff/start_probe.py`. Tooling from the end test
worked as documented, no new failures; probe runs took ~3.3 minutes
total.

---

## 34. Transcript-differential trim + end-pad amendment — IMPLEMENTED AND VERIFIED (2026-11-13)

**Reason:** Entries 32–33 adopted the amendment in principle (trim to
first/last ad-copy word on both edges, +0.50s end-pad, no start-pad)
but deferred implementation until the start boundaries were measured.
The owner directed implementing it.

**Method:** The module (`tmp/transcript_diff/transcript_diff.py`,
661 → 957 lines) now trims each gap: split the gap into speech
segments at 1.5s word-gaps; the first segment with ad evidence sets
the start, the last sets the end; evidence is description-sponsor
mentions (an optional `sponsors` parameter — the pipeline passes the
description sponsors at merge), spoken URLs (a bare TLD token like
"com", or a single token ending in a TLD that contains a dot), and
the word "sponsored". When no segment has evidence, the longest
segment stands in, but only if it holds at least half the gap's
words — otherwise the gap keeps its raw edges. A trailing repeat of
the episode's opening bumper is stripped from the end, and one
trailing 1–2 word segment is absorbed (a closing CTA fragment split
off by a long pause). Start is the exact first ad word with no pad;
end is the last ad word floored to the whole second, then +0.50s.
Gap detection itself is untouched.

**Results (9 Omny ads, sponsors=None — the real Omny case):**
Starts: raw mean error 2.10s → trimmed 0.88s; 6/9 inside 1s → 8/9.
Mid-rolls + outros 6/6 inside 1s, max error 0.04s. Openers: 874 and
875 fixed exactly (0.00s); 873 stays −7.80s copy-based (0.00
region-based) — the promo runs into the copy with only a 0.44s gap,
below every intra-copy pause on the set, so no silence threshold
separates them; no regression. Ends: raw mean error 5.78s → trimmed
0.29s; 5/9 inside 1s → 9/9, max 0.48s — essentially the end test's
bar (0.28s / 0.48s). With sponsors=["Coca-Cola", "BetterHelp"] the
edges are identical on all 9 ads. Unit tests 45/45 pass (26 original
+ 19 new trim checks). Smoke test: PC20-235 live diff still 0 gaps
(coverage 0.9189, 7,214 anchors); the simulated 60s drop is still
exactly one gap (1800.0–1860.5; only the +0.50s pad moves it, +0.2s).
Structural check: a simulated non-ad gap corroborates 0 blocks and
never becomes a marking. Extra check on a fourth episode (Wild at
Heart, 2 real ad gaps): both trim by 0.3s at the end, absorbing the
lone word "Visit" after a 7.9s pause; the ad's true end past "Visit"
is unknowable (the file ends at 808s).

**Two corrections to the earlier probe reports:** (1) The module's
own raw gaps were already better than the probe reports' numbers —
the probe numbers came from the standalone diff script
(whole-sequence difflib), while the module's anchor-chain alignment
matches the Patreon plug words (they ARE in the publisher
transcript), so its raw outro starts were already exact; the trim's
start work was the openers plus preserving those starts. (2) The
Omny descriptions name no sponsors at all — the trim on Omny runs
entirely on generic URL/sponsored evidence plus the longest-segment
fallback; the `sponsors` parameter is wired and tested for feeds
whose descriptions do name sponsors.

**Two bugs found and fixed during verification:** (1) the
single-token URL check false-positived on ordinary words ending in a
TLD ("delicious", "previous") — fixed by requiring a dot in the raw
word; (2) the 7.9s pause before the closing CTA word "Visit" — the
straggler rule was added for it. Both pinned by unit checks.

**Caveat:** the three original show legs (Podcasting 2.0's 32.3s
transcript hole, No Agenda, Bootstrapped Founder) were not re-run —
their episode data is no longer on disk. The amendment is edge-only
and gap detection is proven unchanged by the smoke test, so a leg
that found no gaps still finds none, but that is inference, not a
re-run.

**Status:** ready to fold into the combined patch. INTEGRATION.md §9
records exactly what must change: regenerate the transcript-diff
new-file portion (piece3_td_newfile.diff) from the amended module;
the run_pipeline.py hunks apply unchanged (sponsors is optional),
with a recommended one-line addition to pass the description
sponsors. The combined patch itself was not touched. Records:
`tmp/transcript_diff/verification_log.txt`,
`tmp/transcript_diff/verify_trim.py` +
`tmp/transcript_diff/verify_trim_results.json`,
`tmp/transcript_diff/transcript_diff_pre_trim.py` (backup of the
pre-amendment module).

---

## 35. TTS full-length scale-up — FAKE PODCAST WORKING AS INTENDED (2026-11-13)

**Reason:** The owner directed scaling the TTS pilot to full length and
running the full program against it and one manually marked Unsubscribe
episode, to see if the results are similar. The agreed reading: the
fake scoring somewhat better on edges is the expected cleaner-audio
effect, not a failure; much worse results, especially on recall, would
mean the fake is not working.

**Method:** A 54:30 synthetic episode (Dan and Maya restoring a 1920s
Craftsman house), local Piper TTS, two host voices, disfluencies
written into the script, brown-noise room-tone bed mixed 38 dB below
the speech. 9 ads with exact script truth: 8 same-voice host-read
(Progressive pre-roll 40.4s, Ghostbed, Manscaped, Ridge, AG1, NOBL,
SuperPower, Perfect Jean post-roll) + 1 produced-style different-voice
ad (Leaf Filter, 28.5s). Design cases: one ad under 45s, one under
the 30s floor, one back-to-back pair with zero gap (Ridge + AG1), a
pre-roll and a post-roll. Detector-only on BOTH episodes (no
dynamic-slot B flow), same scorer. The episode came in at 54:30,
short of the 60–75 min target — the script ran 11,988 words. Ad
density is higher than the real comparison episode (473.8s of ads,
14.5%, vs 673s / 9.4% on 286). Room-tone check: 120s sample
transcribed at 0.924 word agreement vs script; the bed did not hurt
transcription and created no false reverb votes.

**Results:** TTS: recall ≥50% 8/9 (0.889), ≥80% 7/9 (0.778), 8
blocks, 0 false blocks, 16 edges matched, 6 within 1s (37.5%), 9
within 2s (56.2%), median edge error 1.56s, mean 3.15s, max 10.63s.
286: recall ≥50% 9/9 (1.000), ≥80% 7/9 (0.778), 9 blocks, 0 false
blocks, 18 edges, 3 within 1s (16.7%), 8 within 2s (44.4%), median
3.02s, mean 4.67s, max 15.94s. Transcription: TTS word agreement vs
script 0.944 (11,058 words transcribed vs 11,787 script words —
Whisper drops/merges disfluencies). Per-ad TTS: Progressive 92.9%,
Ghostbed 79.3%, Manscaped 80.0%, Ridge MISSED (0%), AG1 89.0%, NOBL
98.7%, SuperPower 84.2%, Leaf Filter 99.6%, Perfect Jean 93.2%.

**Where the two diverge:** (1) Edges better on the fake, as expected
(median 1.56s vs 3.02s) — cleaner audio and near-perfect
transcription. (2) Recall one ad lower on the fake: the single miss
is Ridge, the first half of the zero-gap back-to-back pair — the
detector found AG1 (89.0%) but produced no Ridge block at all; the
pair fused and only the AG1 half survived. A known mechanism gap (no
split for back-to-back ads), seen before on the synthetic spliced
episode (Stop Box + SuperPower). A detector gap, not a fake-episode
gap: the same fusion would happen on real audio. (3) Ends skew early
on the fake (Ghostbed −10.63s, Manscaped −9.63s, SuperPower −8.02s,
AG1 −6.09s) — the scripted ad closings contain no brand/URL/CTA
evidence in their final seconds, so refinement pulls the end back to
the last evidence sentence; real 286 ends skew early too but less
sharply. (4) Both under-floor ads were caught: Leaf Filter at 28.5s
scored 99.6% with the best edges (−0.91s / −0.12s); refinement shrank
its block to 29.3s, below the 30s floor — the floors do not bind
after refinement. (5) Brand spelling cleaner on the fake (3–5 exact
mentions per ad window); on real 286, three ads have zero exact
mentions. The fake tests detection logic, not ASR robustness. (6)
Content equally clean on both: zero sponsor mentions and zero .com
tokens in the TTS content; 286 content has 1 .com token in 20,340
words.

**Conclusion:** the fake podcast is working as intended. The results
differ from the real episode in the expected direction and by the
expected amount: edges better (cleaner audio), recall one ad lower
because of a designed hard case with a known detector gap, zero false
blocks on both sides. A valid test bed for detection logic and edge
accuracy, with the standing caveat that it cannot test ASR
robustness. Records: `tmp/tts_full/` (full_episode.mp3, truth.json,
script.txt, description.txt, detect.json, score.json,
score_286_comparison.json, NOTES.md). Build was interrupted twice by
VM reboots; the comparison run finished 15:37–15:44 on 2026-11-13.
Transcription took ~68 min for the 54-min episode under CPU
contention from the validation driver.

---

## 36. Corroboration-gated orphan segments — DISCARDED, all three gate variants (2026-11-13)

**Reason:** The orphan-split test (entry 30) showed the MAX_AD_LEN
drop is load-bearing noise suppression: splitting emits orphan
segments for every content-discussion sponsor. The follow-up idea was
to keep the episode-wide span but only emit an orphan segment when
corroborated — (a) ≥2 mentions inside the span, (b) another method
fires within the segment or ±30s, (c) both gates together.

**Method:** Harness variants under `sb_eval/ab/orphan_gate_a`,
`orphan_gate_b`, `orphan_gate_ab`. Baseline reproduced first in a
fresh combined_repro dir (exact: recall 0.673, median 4.7s,
within-1s 26.4%, FP 36).

**Results (25 episodes, 107 labels):** Gate (a): identical to
baseline on every headline — recall 0.673 (72/107), median 4.7s,
within-1s 26.4%, FP 36. A complete no-op. Gate (b): recall 0.673, FP
36, median 4.7→4.3s, within-1s 26.4%→27.1% — but the whole gain is
a single edge, and that block loses its sponsor label (the
Unsubscribe 287 Stop Box block is relabeled "dry-island"), an
unscored harm. Both gates together: identical to baseline on every
headline. Reason: 45 of 50 orphan segments pass both gates, so the
gates barely fire — the orphan segments that exist are almost all
already corroborated, and the ones the gates would remove are not the
ones causing false blocks.

**Conclusion:** DISCARDED at all three gates. The
corroboration-gated orphan idea is closed: the gates do not separate
true ads from content-discussion sponsors. The variant code was
removed from the scratch copy only; run_pipeline.py was never
touched; no patch was written. Records: `sb_eval/ab/orphan_gate_a`,
`orphan_gate_b`, `orphan_gate_ab` (results.json).

---

## 37. Gate tightening for partial-overlap dynamic slots — MERGE WORKS, patch held separately (2026-11-13)

**Reason:** The owner's decision (2026-11-13): a dynamic slot
overlapping a detector block by less than half the slot's own
duration must not become a standalone block. On 286, the 899.4–927.1
"cha" hallucination slot (27.7s, inside the real Ghostbed ad) passes
the A-side gate because a Ghostbed method segment overlaps it, and
becomes an unnamed standalone block — FP 0→1 at ≥50%.

**Method:** Overlap = overlapped seconds ÷ slot duration, threshold
50%. Two behaviors tested: (a) merge the partial-overlap slot into
the overlapped block (slot edges + detector label), (b) drop the
slot. Verified on 286, 287, SN 1101, and the SB dynamic episodes
(reusing the driver's cached B transcripts read-only).

**Results:** Both merge and drop remove the 286 false positive (FP
1→0, 9/9 at ≥50% held). Merge chosen over drop: merge gives true
coverage 636.3/673s (base 645.3 double-counts 9.0s of overlap between
the "cha" block and the Ghostbed block) and the Ghostbed block
becomes 899.4–1006.7 labelled GHOSTBED, with the start error
improving from +11.1s to −7.6s; drop keeps the detector's 918.1–1006.7
block and loses the first 11.1s of the real Ghostbed ad. 287
unchanged under both: 8/8, 1 false positive (Pepperbox), 7/8 at
≥80%. Dynamic edges unchanged: 14 edges, mean error 0.43s, 13/14
within 1s. The 58-line patch applies cleanly to a fresh copy,
compiles, and is byte-identical to the tested copy; the live file is
unchanged.

**Status:** patch held separately — NOT in the combined patch; the
two apply independently in either order. Records: `tmp/gate_fix/`
(gate_fix.patch, NOTE.md, verification_log.md,
patch_check_286.json).

---

## 38. Back-to-back ad split (rescue) — WORKS, FAILS THE LETTER OF THE DECISION RULE, OWNER'S CALL (2026-11-13)

**Reason:** The TTS full-length comparison (entry 35) exposed the
fusion as the one clear detector gap: the zero-gap Ridge+AG1 pair
fused and only AG1 survived. The synthetic episode had the same
failure (Stop Box+SuperPower fused into one block). No split
mechanism exists.

**Diagnosis (from the pipeline's own logs):** Both pairs share one
mechanism in three steps: (1) one heat-map region spans both ads
because votes never drop below 2 across the boundary; (2) the label
picker names the whole region for one sponsor; (3) the mention-span
cap + edge refinement, both anchored on the picked sponsor's
mentions, trim the block to that sponsor alone — erasing the other
ad. TTS Ridge+AG1: region 1731–1863s; AG1 wins the picker
alphabetically (both sponsors have in-region mentions); the Ridge cta
segment (56.8s) is skipped as "already covered"; the Ridge cluster
segment (40.8s) is under the 45s single-method floor. Synthetic Stop
Box+SuperPower: region 5920–6075s; Stop Box has NO method segment at
all — its variants include bare "stop", so the leaky find_mentions
matches "stopping"/"Christopher" episode-wide, the orphan span blows
past the 720s MAX_AD_LEN gate and is silently dropped; no spoken URL
and only 2 real mentions, so no CTA/cluster either. Note: the held
substring-fix patch would let the orphan emit, but the fused block
would still be labeled for one sponsor only — the fusion would flip,
not resolve — the rescue is still needed after that fix.

**Method:** A post-processing rescue (harness variant only): rebuild
the raw heat-map region for each final block; if the region extends
≥30s past the block on either side and that extension holds ≥2
mentions of a *different* description sponsor, cleanly ordered
against the block sponsor's mentions (all ending before / starting
after), emit a rescue block. Edges come from the other sponsor's
cluster segment if any (else earliest cta/brand/duplicate/chapter
segment, else the region edge), bounded to its mention span ±30s; the
shared edge is the block edge refinement already placed. Guards: ≥2
mentions, span ≥5s, strict ordering, the other sponsor must not
already have a block there, ≥30s, clipped against neighbours. One
fix found during testing: mentions are counted in a window extended
30s outward from the region — Stop Box's first mention (5918.8s)
sits 1.2s *before* the region start (5920s), and without the
look-back the rescue never fired.

**Results — the two known pairs:** TTS Ridge: coverage 0.0 → 0.978,
block 1746.78–1795.8, errors +1.07s/+0.53s; AG1 unchanged. TTS
overall: 8/9 → 9/9 at ≥50%, 7/9 → 8/9 at ≥80%, FP 0 → 0, median
1.56s → 1.32s, within-1s 37.5% → 38.9%. Passes the decision rule
outright. Synthetic Stop Box: coverage 0.0 → 0.938, block
5920–5968.7, errors +3.1s/+1.1s; SuperPower unchanged. Synthetic
overall: 22/27 → 23/27 at ≥50%, 18/27 → 19/27 at ≥80%, FP 3 → 3,
median 2.55s flat, within-1s count 8 flat (18.2% → 17.4% on
denominator growth). Null episode: 8 blocks both, no rescue.

**Results — SponsorBlock set (25 episodes, 107 labels):** Baseline
repro exact. Variant: recall 0.673 → 0.692 (72 → 74 of 107), FP 36
→ 39 (+8.3%), median 4.7 → 4.8s, within-1s 26.4% → 25.7% (count
flat at 38). 21 of 25 episodes byte-identical; 5 rescue blocks fired
across 4 episodes, all adjudicated: two matched SB labels (Helix
Sleep, Eight Sleep — both pairs SB had marked missed); two are
second halves of pairs SB labels as single segments, landing just
under the 50% match line only because the first half already matched
the same label (49.2%, 46.7%); one is a real ad in an SB label gap
(lex-499 Shopify — verified by reading the transcript: the opening
is five back-to-back ads, SB labels none of the first 2732s). Zero
of the five is a content false block.

**Verdict:** FAILS the letter — median 4.7→4.8s and within-1s
26.4%→25.7% — so by the standing rule it is a DISCARD. But the
failure is a measurement artifact, quantified: the two matched
rescues contribute start edges of +77.3s and +72.7s because the SB
label starts at the *first* ad of the pair while the rescue correctly
starts at the split point. Variant minus those two edges: median
4.70s (identical to baseline), mean 11.91s (better than baseline
12.02s). SB structurally cannot represent back-to-back pairs, so a
correct split is scored as a 70s+ start error. The detector is
strictly better with the mechanism (2 missed SB labels recovered, 0
content false blocks added). **Decision 2026-11-14: the owner kept it
despite the letter.** If kept, the SB baseline becomes recall 0.692 /
FP 39 by SB counting. Known limitation: one
rescue per side per block — lex-499's Element/BetterHelp reads stay
unmarked and the NetSuite read sits inside the Shopify-labeled
block; a future step could iterate the rescue over a block's tail.

**Status:** 306-line patch (split_rescue.patch: new
pipeline/split_rescue.py + 13-line wiring hunk applying the rescue
after the slot flow) KEPT by the owner's decision 2026-11-14; being
folded into the combined patch as piece 5 (same treatment as the gate
fix), still held until merge day. Verified: applies cleanly to a
fresh pristine copy, both files compile, patched module reproduces
the tested rescue, anonymization-clean.
The live pipeline was never touched. Records:
`sb_eval/ab/back_to_back_split/` (split_rescue.py, NOTE.md,
tts_results.json, synthetic_results.json, null_results.json,
results.json, split_rescue.patch).

---

## 39. Density / ad-copy floor for standalone slot blocks — DENSITY KEPT AS A CORRECTNESS FIX, COPY DISCARDED (2026-11-14)

**Reason:** The 8 extra slot blocks from the dynamic-slot validation
were adjudicated by hand: 6 real paid dynamic ads, 1 self-promo, 1
non-ad outro tail (Mitochondria, 123:36–124:16, density 0.176, 3
differing words). The slot flow emits standalone slot blocks with no
density or ad-copy requirement. The question: can a floor remove the
outro tail without removing real ads?

**Design, from the data:** Across all 26 gated slots measured (SB
set 18 + 286/287 8), densities are 0.176 for the one false slot and
0.677–1.0 for everything else. The next-lowest real ad is LinkedIn
at 0.677. Threshold 0.5 sits in the middle of that gap. The 286
"cha" hallucination slot is 0.969 — density does not catch it (the
held gate fix does). Ad-copy evidence (brand mention or spoken URL
in the A-side words, using the pipeline's own pools and finders):
LinkedIn, HubSpot, the Sydney promo, and Progressive carry at least
one; Mitochondria carries neither — but so do Netflix and both
RaceTrac ads (their brands are in no pipeline pool — dynamic
sponsors are not description sponsors — and they speak no URL).
Trailing-only was considered and rejected: 5 of the 6 real ads are
trailing too (the outro is a rotating dynamic-ad position), so
trailing-ness does not separate them. The floor applies to ALL
standalone slot blocks.

**Results — SB set (25 episodes, 107 labels):** Baseline repro exact
(recall 0.673, matched 72/107, FP 44, median 4.7s, mean 12.0s,
within-1s 26.4%). D (density ≥ 0.5): recall 0.673, matched 72/107,
FP 44 → 43, median 4.7s, mean 12.0s, within-1s 26.4% — every
headline flat except FP; removes ONLY the Mitochondria slot. C (copy
only): FP 44 → 40, all else flat — but removes Mitochondria AND both
RaceTrac ads AND the Netflix ad. DC (both): identical to C on every
number (FP 40; same 4 removals).

**Per-slot outcomes (all 8 adjudicated slots):** D keeps LinkedIn
(0.677), both RaceTrac (0.897, 0.878), HubSpot (0.950), Netflix
(0.868), Sydney promo (1.000), Progressive (0.912); removes only
Mitochondria (0.176). C/DC remove Mitochondria plus both RaceTrac
and Netflix.

**286/287:** D leaves both unchanged — 286 score50 (9, 9, 1),
score80 (9, 9, 645.3, 673, 27.0, 0); 287 score50 (8, 8, 1), score80
(7, 8, 477.3, 531, 41.7, 1). The "cha" slot (density 0.969) is kept
under D. C/DC: 286 score50 FP 1 → 0 (removes "cha"), score80
coverage 645.3 → 625.2; 287 unchanged. That does not save them —
the SB-set real-ad removals decide it.

**Verdict:** D: KEEP, as a correctness fix (not a detection
improvement). It passes the adjudication check (removes the
Mitochondria tail, keeps all 6 real ads) and every decision-rule
condition except the median letter: median 4.7 → 4.7s (flat),
within-1s 26.4% (holds flat), recall flat, FP 44 → 43. The median
cannot move — removing a false block moves no matched edge (all 144
edges identical). Same shape as the substring fix (entry 31), kept
as a correctness fix on exactly that reasoning: provably-false
evidence removed with zero regressions on any check. C: DISCARD —
removes 3 real ads; fails the adjudication check regardless of the
SB numbers (which look better — FP 40 — precisely because SB never
labels dynamic ads). DC: DISCARD — same 3 real ads removed; the
density half adds nothing over C.

**Status:** 26-line patch (density_floor.patch: adds
SLOT_FLOOR_DENSITY_MIN = 0.5 and a density check in _merge_slots'
standalone branch) HELD — the owner's call whether it folds in as
piece 6 before merging. Verified: applies cleanly to a fresh
pristine copy (dry-run + apply + py_compile) and to the 5-piece
combined-patch tree (hunk 2 at +211 lines offset, no fuzz); the
patched _merge_slots reproduces config-D scores exactly on the
frozen 286/287 merge inputs. Anonymization-clean. Caveat: the floor
was tested against the dynslots baseline (FP 44); on the folded
5-piece patch the SB-set FP is 38, so the expected effect there is
the same single-block removal (FP 38 → 37) — to be confirmed at
fold time. The floor does not touch the 286 "cha" slot (density
0.969); that false block is removed by the held gate fix — the two
mechanisms are complementary. Records: `sb_eval/ab/dynslots_floor/`
(dynslots_floor.py, NOTE.md, density_floor.patch),
dynslots_floor_results.json, `combined_repro23/`,
`dynslots_floor_d|_c|_dc/`.

---

## 40. Pre-roll-only history voter (leave-one-out) — DISCARDED under the decision rule (2026-11-14)

**Why it was a candidate:** the per-podcast history file (entry: built
2026-11-14, 129 rows / 26 episodes / 8 shows) showed only two shows
with a tight, threshold-met pre-roll: Huberman Lab (8 eps, slot 1 at
9.9% ±2.12) and Security Now (5 eps, slot 1 at 3.06% ±2.39).
Unsubscribe had the tightest grid (slots 1–9 std ≤3.88) but only 3
episodes — below the 5-record gate. The full position prior was not
supported; this was the narrowest viable first voter.

**Design (frozen before the run, in `sb_eval/ab/history_preroll/NOTE.md`):**
slot 1 only; gate ≥5 episodes in the show's history file; leave-one-out
prior (slot-1 mean/std of start_pct from the loader, population std,
the held-out episode's rows excluded by episode_id); window = mean ±
max(2σ, 3.0 points), clipped to [0, 100]; one heat-map vote (method
`history-preroll`, label `history-prior`, artifact-classed so it can
never name a block, never in `_single_methods`, never a suspect —
corroboration-only by construction). Implemented as a patched copy of
the live pipeline (`sb_eval/ab/history_preroll/pipeline_patched/`, 3
edits), driven by `run_variant.py`. Smoke-tested first: vote off,
patched == frozen exactly; vote on, no block ever labeled
`history-prior`.

**Baseline reproduction (fresh `combined_repro25`, frozen copy of the
live pipeline): exact** — recall 0.701 (75/107), FP 38, median 4.8s,
mean 13.2s, within-1s 26.0%, identical to `merged_live` on every
headline, per-show, and per-episode detect.json.

**Results (full 25-episode SB set):** recall 0.701 → 0.701 (75/107),
FP 38 → 38, median 4.8s → 4.8s, mean 13.2s → 13.1s, within-1s 26.0%
→ 26.0%, within-2s 36.7% → 36.7%, max 84.9s → 79.3s. Huberman Lab:
recall 0.75 flat, median 0.8s flat, mean 16.7 → 16.6s, within-1s
52.1% flat. Security Now: every headline identical. Matched-edge
deltas: 1 better / 1 worse / 74 unchanged. Silence verified: 12
episodes below the gate (Unsubscribe 3, Modern Wisdom 4, Vergecast 3,
99pi/Acquired/Lex 1 each) have block-identical variant output. The
voter fired on 13 episodes (8 Huberman, leave-one-out n=7, slot-1 mean
9.57–10.56%, std 1.29–2.26; 5 Security Now, n=4, mean 2.18–3.75%, std
1.81–2.65); windows ran ~[5.3%, 14.5%] on Huberman and [0%, 5.8–8.2%]
on Security Now. 9 of the 11 visibly affected episodes changed only
in the method list (`history-preroll` joins, spans unchanged).

**Adjudication of the two real changes (both Huberman, both inside SB
labels that merge a back-to-back pair into one):** GAIN — Our Place on
true-happiness (1056.0–1121.3), a real sponsor read the baseline
missed; the history vote extended the Helix Sleep region's head and
the split rescue split the Our Place head off. It counts as an SB
false positive only because the label was already "used" by the Helix
block (one-to-one matching) — the same measurement artifact as the
split rescue's own test. LOSS — Rorra on neuroscience-of-emotions
(1010.9–1097.3), a real sponsor read the baseline marked; the history
vote bridged the 5-second vote dip between the Helix Sleep and Rorra
heat-map regions (1039→1044), fusing them into one 926–1106s region,
and the label picker named it Helix Sleep. The split rescue — built
for exactly this fusion — could not fire: it is mention-gated, and
Rorra has zero findable mentions (Whisper hears "Rora", the sponsor
variants are `['rorra']`). Net adjudicated effect: +1 real ad, −1 real
ad — a wash, with a new structural harm (the ~600–900s-wide prior
window bridging inter-ad dips) that the existing rescue cannot always
repair.

**Conclusion: DISCARD.** Median flat and within-1s flat — fails the
letter of the decision rule. More data would not resolve it: the harm
is structural (window width vs. inter-ad dips), not sample size; the
gain is real but small (1 ad in 13 fired episodes). The history file
itself stands (the collector is merged; the data is sound) — only this
voter in this form is discarded. Two paths forward: (1) narrow the
window (±1σ instead of ±2σ, or cap the width) and re-test, to see
whether the corroboration gain survives without the fusion harm; (2)
fix the fusion interaction first (make the split rescue see
URL-anchored sponsors like Rorra, or guard the history vote against
bridging a vote dip between two different-sponsor regions), then
re-test. Records: `sb_eval/ab/history_preroll/` (NOTE.md,
run_variant.py, run_baseline.py, priors.json, priors_run.json,
results.json, `combined_repro25/`).

Caveats: the live pipeline md5 changed mid-build (collector merge,
`6d19861772…` → `bc971d12f812…`); the frozen copy was diffed against
the fold6 s7 tree and the only differences are the collector's
additions — detection-identical. The first variant run wrote run-time
priors to `priors.json`, clobbering the frozen table; restored from the
pre-run copy, run-time file saved as `priors_run.json`, and the build
script now dumps there. SN leave-one-out priors use 4 episodes (the
gate is on the 5-episode file count).

---

## 41. URL-anchored split rescue + history-vote bridging guard — FAILS THE LETTER, ADJUDICATED +2 REAL ADS, OWNER'S CALL (2026-11-14)

**Why it was a candidate:** entry 40's pre-roll voter was discarded
because of one structural harm — on neuroscience-of-emotions the
history vote bridged a 5-second dip between the Helix Sleep and Rorra
regions, fusing them, and the split rescue could not fire because
Rorra has zero findable mentions (Whisper hears "Rora", the sponsor
variants are `['rorra']`). Rorra's block did carry `cta/url` evidence.
Two candidate fixes, both frozen in `sb_eval/ab/fusion_fix/NOTE.md`
before any test run, both generic (evidence-driven, no hardcoded
names):

(a) **URL-anchored split rescue** (`split_rescue_url.py`, modified copy
of live `split_rescue.py`): tried only when the mention-gated search
finds no candidate. Per side, a stem X qualifies if a `url` candidate
overlaps the extension by ≥30s AND a `cta` candidate with the same stem
X overlaps by ≥30s, X fuzzy-matches a description sponsor
(SequenceMatcher ≥0.85, so `rora` vs `rorra` = 0.889), the rescue
sponsor differs from the block sponsor, and the block sponsor has no
mentions inside the extension. Tail edges: start = block end, end =
url segment end clipped to region end. Head edges: start = cta segment
start clipped to region start, end = block start.

(b) **History-vote bridging guard** (live copy + voter method + guard):
inserted in `detect_ad_blocks` just before the heat-map build. It
computes baseline regions (≥2 votes) from non-history candidates and
their labels via the pipeline's own `_pick_label`; for each pair of
adjacent baseline regions with DIFFERENT labels, if every second of
the gap already carries ≥1 vote (so the history vote would connect
them), it punches a hole in the history segment over the gap.
Otherwise the history segment is untouched — the Our Place gain is
safe because its evidence at 1055–1089 sits in a gap whose 1040–1054
seconds carry 0 votes.

**Baseline reproduction (fresh `combined_repro26`): exact** — recall
0.701 (75/107), FP 38, median 4.8s, mean 13.2s, within-1s 26.0%
(39/150), within-2s 36.7%, max 84.9s; identical to `merged_live` on
every headline AND 0 episodes differing in blocks+suspects.

**Fix (a) alone (`fusion_rescue_url`):** recall 0.701 flat, FP 38 →
39 (+2.6%), median 4.8s flat, mean 13.2s flat, within-1s 26.0% flat,
max 84.9s flat; every show identical to baseline on
recall/median/within-1s. The URL anchor fired exactly once in 25
episodes: Rorra tail rescue 919.7–999 on Huberman
what-humans-animals-and-plants-tell-us-a (from the Eight Sleep block,
region [832, 999], anchor `url`). Transcript-verified a REAL Rorra
read ("Today's episode is also brought to us by Rora… Rora.com slash
Huberman"), the same script as the neuroscience episode. SB counts it
as an FP because the label 832.6–999.0 is one merged pair label
already matched by Eight Sleep (rescue overlap 79.3s = 47.7%, below
the 50% threshold). Rescue regression (stock vs URL rescue):
synthetic 23/27 ≥50%, 19/27 ≥80%, FP 3, median 2.55s, Stop Box
rescued 0.938 — identical; null 8 blocks — identical; TTS 9/9 ≥50%,
8/9 ≥80%, FP 0, median 1.32s, Ridge rescued 0.978 — identical. The
URL path adds nothing to the mention-anchored cases and breaks
nothing. **Decision-rule verdict: FAILS the letter** (median and
within-1s flat). Adjudicated: **+1 real ad, 0 lost, 0 false firings**
— the same shape as the split-rescue precedent (entry 38).

**Fix (b) + voter (`history_guard`):** recall 0.701 flat, FP 38 →
39 (+2.6%), median 4.8s flat, mean 13.2 → 13.1s, within-1s 26.0%
flat, max 84.9 → 79.3s; every show identical to baseline on
recall/median/within-1s. Neuroscience: Helix 926.3–1018.9 AND Rorra
1010.9–1097.3 — the Rorra block is identical to baseline; the fusion
is prevented. Heat-map proof: the guard run has two regions 926–1040
and 1044–1106 (hole punched over the 1040–1044 dip); the voter-alone
run has one fused region 926–1106. True-happiness: Our Place
1056.0–1121.3 AND Helix 1121.3–1241.2 — identical to the voter-alone
run; the gain survives. All other block diffs vs baseline are
label-string-only; the 12 silent episodes are byte-identical.
**Decision-rule verdict: FAILS the letter.** Adjudicated: **+1 real
ad (Our Place), 0 lost (Rorra kept).**

**Combination (a) + (b) + voter (`history_guard_url`):** recall 0.701
flat, FP 38 → 40 (+5.3%, within the ≤~10% allowance), median 4.8s
flat, mean 13.2 → 13.1s, within-1s 26.0% flat, max 84.9 → 79.3s.
Span diffs vs baseline are exactly three, nothing else: neuroscience
Helix start 950 → 926.3 (voter head-extension) with Rorra kept at
baseline edges; true-happiness Our Place added 1056.0–1121.3;
what-humans-animals Rorra added 919.7–1001.0 (URL anchor fired; the
2s past the 999 read end is the history vote extending the region
end, not the url segment). **Decision-rule verdict: FAILS the
letter.** Adjudicated: **+2 real ads (Our Place, Rorra on
what-humans-animals), 0 lost, 0 false rescues.** SB counting cannot
see any of the three gains: each is the second ad of a merged-pair SB
label already matched by the first ad.

**Conclusion: the numbers fail the letter of the decision rule on all
three variants (median and within-1s flat), but the failure is the
same SB measurement artifact as entry 38 — the gains are second ads
of merged-pair labels. Adjudication shows +2 real ads, 0 lost, 0
false rescues for the combination. MERGED 2026-11-14 by the owner's
direction ("merge both"), same precedent as the split rescue.**
Post-merge verification on the live tree (`sb_eval/ab/merged_live_fusion/`,
driver `sb_eval/ab/fusion_fix/run_live_verify.py`) reproduces the
tested combination exactly: recall 0.701 (75/107), FP 40, median 4.8s,
mean 13.1s, within-1s 26.0%, max 79.3s; the three span changes
confirmed and no others; 286/287 unchanged (286 7/9 ≥80%, 606.7/673s,
0 false; 287 6/8, 463.0/531s, 1 false); synthetic/TTS/null regression
identical. New live md5s: run_pipeline.py `3ee571ba5d6fd4191ba130a4cd563cb8`
(3,928 lines), split_rescue.py `1565b3253d7bf075bbb7abe5b7d294b4` (376
lines). One verification lesson: the merge agent's first run scored FP
39 not 40 because its driver imported the stock `split_rescue.py` from
`sb_eval/ab/back_to_back_split/` instead of the live pipeline copy — a
shadowed import fails silently with plausible-looking numbers, so a
verification driver must assert which module file it imported (print
`__file__`). Held patches, both verified (dry-run
+ real apply clean to a fresh copy of the live pipeline, compile,
byte-identical to the tested files): `rescue_url.patch` (170 lines,
fix (a) only, against live `pipeline/split_rescue.py`) and
`voter_guard.patch` (140 lines, voter method + guard, against live
`pipeline/run_pipeline.py` — the guard is part of the voter feature,
never folded into the fix patch; the combination was tested as the two
patches applied together). Records: `sb_eval/ab/fusion_fix/`
(NOTE.md, split_rescue_url.py, rescue_url.patch, voter_guard.patch,
run logs, synthetic_results.json, tts_results.json, null_results.json),
`sb_eval/ab/fusion_rescue_url/`, `sb_eval/ab/history_guard/`,
`sb_eval/ab/history_guard_url/`, `sb_eval/ab/combined_repro26/`,
`sb_eval/ab/merged_live_fusion/`.

Correction recorded: the NOTE.md claim that the (a) Rorra rescue on
neuroscience overlaps the SB label by 90.7% and matches at ≥50% was
wrong — it used the truth block (86.4s) as the denominator. Against
the SB label (925.9–1097.7, 171.8s) the overlap is 78.8s = 45.9%,
below threshold; the Helix block (92.6s = 53.9%) takes the label
instead. The rescue restores a real ad SB scoring cannot see. The
NOTE.md is corrected.

---

## 42. Weak-evidence block labels — RECOMMEND MERGE, MERGED 2026-11-18 (autonomous run, Test 4)

A block whose label rests only on weak methods (duplicate, reverb,
voice, history — never a naming method) gets no sponsor name in its
chapter title (plain "Muse Detected Ad") instead of a made-up one.
Detection untouched by construction: the strip runs after spans and
votes are final, and downstream steps read only whether a label part
matches a description sponsor — 'ad' matches none, exactly as
'dry-island' matched none. Baseline reproduced exactly first
(`combined_repro30`). Variant: every detection number identical, 0
span changes on all 25 episodes, exactly 2 label changes (Acquired's
"Dry-Island" block and SN-1103's "duplicate-9" block become plain).
On Ep 289 the same rule changes exactly the 3 Dry-Island blocks
(38:05, 55:25, 70:25). Transcripts confirm all five weak blocks are
real ad audio with fictitious names: 289's three are the same
repeated dynamic ad, a Universal Pictures "Focker and Law" movie
promo; Acquired's is a Sierra read; SN-1103's is a Wayfair Christmas
read. Wrong chapter names removed: 4 total (1 on the SB set + 3 on
Ep 289). Correct names removed: 0. (The SB-set effect looks small
because SB labels are host-read-only; 30% of a published episode's
chapters carried a fictitious name.) Held patch:
`sb_eval/ab/label_quality/label_quality.patch` (3,161 bytes).
Records: `sb_eval/ab/label_quality/`. Merged 2026-11-18 at the
owner's direction; applied cleanly first try. Post-merge
verification: the 2 SB-set label changes confirmed, 0 span changes,
all headlines identical (see the merge record at the end of entry
44).

## 43. URL-stem guard — RECOMMEND MERGE, MERGED 2026-11-18 (autonomous run, Test 6)

When a block's sponsor name comes from the fuzzy URL-stem fallback,
reject it if the stem matches the podcast's own identity
(feed-supplied channel domain stem plus own domains — never
hardcoded). Baseline reproduced exactly first
(`combined_repro32`). Variant: detection identical, 0 span changes,
exactly 3 label changes — both Lex Fridman blocks ("lexfreedman"
and "friedman", both chaptered "Lexfridman") and one Huberman outro
block ("hubertmanlab") become plain "Muse Detected Ad". Label
accuracy: wrong 1 → 0, unnamed 0 → 2, unknown 1 → 1. The correct
count dips 73 → 72, but that is the frozen-adjudicated Lex twin —
the scorer counted it correct only circularly; the show is not its
own sponsor, so no real sponsor name was lost. The Vergecast case
is unchanged (it goes through a different step — verbatim return,
not the fuzzy fallback — and needs its own mechanism; see Test 7).
Held patch: `sb_eval/ab/url_stem_guard/url_stem_guard.patch`
(4,084 bytes). Records: `sb_eval/ab/url_stem_guard/`.

Merge, 2026-11-18: the original patch failed on hunk #2 because
patch 1 (entry 42) had inserted its call at the same point — just
before `return scored, suspects` at the end of `detect_ad_blocks`.
Regenerated as `url_stem_guard_r2.patch` against the patch-1-applied
file; verified its added/removed lines are identical (as multisets)
to the original — same helpers, same `own_identity=None`
parameter, same call logic, nothing else. Dry-run clean, applied.
Wiring (part of the documented merge form): `process_episode` and
`slots_only_mode` pass
`own_identity={_domain_stem(host)} | own_domains` to
`detect_ad_blocks` — but the NOTE's literal form
`_domain_stem(channel_link)` was wrong: `_domain_stem` takes a
host, and passing the full feed URL gives "https://lexfridman",
which fails the 0.75 fuzzy threshold (0.692 for lexfreedman, 0.609
for friedman; the correct stem `lexfridman` scores 0.857 / 0.778),
so the guard would have been silent in production for its flagship
case (production-path check on frozen Lex inputs: 0/2 blocks
stripped). Fix: extract the host with the same regex
`extract_own_domains` uses before stemming, guarded for a
missing/None link (`parse_channel_link` can return None;
`_domain_stem(None)` would crash). The fixed form yields
`{'lexfridman'}` and strips 2/2. The SB verification passed because
the harness computes `own_identity` with host extraction — it never
exercises the call-site expression, which is why the separate
production-path check (`tmp/prod_path_check.py`) was needed.
Post-merge verification: the 3 SB-set label changes confirmed, 0
span changes, all headlines identical (see the merge record at the
end of entry 44).

## 44. Missed-sponsors check counts blocks as found — RECOMMEND MERGE, MERGED 2026-11-18 (autonomous run, Test 11)

The missed-sponsors check reported a description sponsor as missed
when it had zero transcript mentions, returning before ever looking
at detector blocks — so a sponsor whose read Whisper mis-transcribed
to zero mentions was flagged missed even when a block names it
(exactly the FUM on Ep 289 case: block at 61:00 names FUM,
transcript says "fume" / "tryfume"). The fix, in the zero-hit branch
only, applies the same block-label test the check already uses on
the mentions-found path. No new matching rules, no new constants, no
hardcoded names. Baseline reproduced exactly first
(`combined_repro36`). Frozen diagnosis: 38 missed sponsors total (37
SB set + 1 on Ep 289), exactly 2 false alarms: FUM on Ep 289 and
Rorra on Huberman "What Humans, Animals and Plants Tell Us About
Consciousness" (transcript says "Rora" throughout; block 919.7–
1001.0 contains the whole read with its rora.com CTA). Variant
result exactly as predicted: detection identical (recall 0.701, FP
40), spans identical block-for-block on all 25 episodes, missed
sponsors 38 → 36 — exactly the 2 false alarms removed, 0 true
misses lost, 0 newly flagged. Rorra is still correctly flagged on a
different Huberman episode ("Neuroscience of Emotions"), which has
no naming block. Diagnostic-only — cannot move any span or block by
construction. Held patch: `sb_eval/ab/missed_check/missed_check.patch`
(983 bytes). Records: `sb_eval/ab/missed_check/`. Merged 2026-11-18
at the owner's direction; applied cleanly (hunk at offset +124
lines). Post-merge verification: missed 38 → 36 confirmed.

Label-fix merge (2026-11-18, owner-directed): all three patches
live, run_pipeline.py md5 `8aa885fe680f487a017ada68c616619d`, 4,070
lines (was `44e64847…`, 3,929 lines). Combined post-merge
verification on the live modules (`sb_eval/ab/merged_live_labels/`):
SB set recall 0.701 (75/107), FP 40, median 4.8s, mean 13.1s,
within-1s 26.0%, within-2s 36.7%, max 79.3s — identical to the
pre-merge baseline on every headline and per-show number; exactly 5
label changes, 0 span changes. 286/287 unchanged (286 7/9 ≥80%,
606.7/673s, 0 FP; 287 6/8, 463.0/531s, 1 FP Pepperbox). Synthetic
23/27 ≥50%, 19/27 ≥80%, FP 3, median 2.55s; TTS full 9/9 ≥50%, 8/9
≥80%, FP 0, median 1.32s; null episode 8 blocks — all identical to
frozen numbers. Automatic run stays paused.

---

## 45. Full-pipeline re-measurement — FIRST DIRECT MEASUREMENT (2026-11-18)

The numbers reported for the pipeline were the detector path only
(`detect_ad_blocks` + rescue). The last full-pipeline figures were
stale — pre-fusion-merge, on a 38-detector basis: slot-flow FP 43,
full-pipeline FP 45 (the 45 derived arithmetically, never measured
directly). This run scores all four paths on the frozen 25-episode
SB set (107 labels) with the current live code
(run_pipeline.py `8aa885fe…`, split_rescue.py `9323713c…`), frozen
data only — nothing downloaded or re-transcribed. Baseline
reproduced exactly first (`combined_repro37` equals
`merged_live_labels` on every headline, edge stat, per-show, and
per-episode number).

| Path | Recall | FP (raw) | Median | Mean | Within-1s | Within-2s | Max |
|---|---|---|---|---|---|---|---|
| Detector (detect + rescue) | 0.701 (75/107) | 40 | 4.8s | 13.1s | 26.0% | 36.7% | 79.3s |
| Detect-only (diagnostic) | 0.682 (73/107) | 35 | 4.7s | 12.4s | 26.7% | 37.7% | 79.3s |
| Slot-flow (detect + slots) | 0.682 (73/107) | 42 | 4.7s | 12.4s | 26.7% | 37.7% | 79.3s |
| Full (slots + rescue) | 0.701 (75/107) | 47 | 4.8s | 13.1s | 26.0% | 36.7% | 79.3s |

Decomposition. Rescue: +2 matched, +5 FP — all 5 FP rescues are
adjudicated real ads (entries 38/41: Eight Sleep, Lingo, Our Place,
Rorra, Shopify). Slot flow: +0 matched, +7 FP, and zero
matched-edge changes on any of the 25 episodes — its edge
refinements landed only on blocks SB counts as false positives
(real dynamic ads without SB labels). Where owner ground truth
exists the slot edges are closer (unsub-287 Progressive end −3.7s
→ 0.0s exact; LeafFilter ends −6.1s → +0.9s, −1.7s → +0.5s), but
SB scoring cannot see it (unsub-287 has only 5 SB labels: no
Progressive pre-roll, no Leaf Filter).

Adjudication of the 7 standalone slot blocks (re-read from the
frozen A transcripts; confirms the 2026-11-13 verdicts): 6 real paid
dynamic ads — LinkedIn (8126.7–8166.3), RaceTrac ×2 (9223.3–
9252.7, 8412.1–8451.1), HubSpot (8454.6–8487.7), Netflix
(8433.6–8463.1), Progressive pre-roll (1.0–25.7) — plus 1
self-promo, the Sydney live-show promo (0.0–21.4, house-plug-like).
The Mitochondria tail is gone, removed by the density floor. Four
of the seven carry label None and become plain "Muse Detected Ad"
chapters in production.

Corrected false-block counts (same convention as 2026-11-13 —
correct only the extras): detector 35, slot-flow 35 (36 counting
Sydney), full 35 (36 counting Sydney). After adjudication the full
pipeline has 35 true false blocks — the same as the detector alone;
slot flow and rescue add zero true false blocks. Caveat: 35 is an
upper bound — at least 8 baseline FPs are known real ads SB does not
label (unsub-287 ×3, unsub-266 ×4 Progressive, Acquired Sierra).

Comparison to the stale figures. Slot-flow FP 43 → 42 (−1): slot
net effect unchanged at +7; the −1 is the detector baseline moving
36 → 35 (Mint Mobile phantom gone; neuroscience Helix now matches
its pair label via the voter extension + guard). Full-pipeline FP 45
→ 47 (+2): first direct measurement — the stale 45 was derived
arithmetically. The +2 is the fusion merge's rescue blocks
(detector 38 → 40); slot net +7 unchanged. Corrected full FP 37 →
35 (38 → 36 counting Sydney): Mitochondria tail removed by the
floor (−1) and Mint Mobile phantom gone (−1).

Per-show recall / FP (detector | detect-only | slot-flow | full):
99pi 1.000/0 across all paths; Acquired 0.000/5 across all;
Huberman 0.750/14 | 0.688/10 | 0.688/13 | 0.750/17; Lex 0.500/2 |
0.500/1 | 0.500/1 | 0.500/2; Modern Wisdom 0.625/3 | 0.625/3 |
0.625/6 | 0.625/6; Security Now 0.769/9 across all; Unsubscribe
1.000/7 | 1.000/7 | 1.000/8 | 1.000/8; Vergecast 0.500/0 across
all. Transcript-diff is a verified no-op on the SB set (no
`transcript_tags` in any frozen episode data file); xep was not run
in the harness (verified separately, merge_verify §3). Frozen
inputs: 18 gated slots across 11 episodes (probe.json: 12 dynamic,
10 static, 3 b_failed); b_failed/static episodes are slot-flow
no-ops, same as production degradation.

Note: the `merged_live_labels` driver asserts run_pipeline md5
`9c87823e`, a value no file on disk now has. The live file was
modified 4 minutes after its `results.json` was written (22:14 →
22:18 UTC Nov 18), consistent with the wiring-form fix landing
after its SB verification ran. This re-measurement used the current
live file (`8aa885fe…`) and reproduces its numbers exactly,
confirming the difference is immaterial to SB scoring (the harness
never exercises the call-site expression). Records:
`sb_eval/ab/full_pipeline_remeasure/` (NOTE.md, results.json,
run.log, run_remeasure.py), per-path dirs `combined_repro37/`,
`fp_detect_only/`, `fp_slotflow/`, `fp_full/`.

---

## 46. False-block adjudication — TRUE COUNT IS 1 (2026-11-18)

The re-measurement's corrected count of 35 was an upper bound: it
corrected only the extra blocks (rescue + slot) and took the
detector baseline's false blocks as counted. This run hand-checks
all 35 against the frozen A transcripts (exact in-span words, no
margin) to find the true count. The 35 are the detect-only path's
false_positive_blocks (`fp_detect_only/results.json`) — the same 35
that remain in the detector path (40 raw minus the 5 adjudicated
rescue blocks) and the full path (47 raw minus 5 rescue minus 7
slot blocks).

Verdicts: **32 real paid ads, 2 house-plug-like, 1 true false
block.** The true false-block count is **1** (4 strict, counting
self-promotion as false: the 1 true block, the 2 house-plug-like
detector blocks, and the Sydney live-show promo among the 7
standalone slot blocks). Comparison: raw 47 → corrected 35 →
**true 1 (4 strict)**.

The 32 real ads are full sponsor reads with brand names, spoken
URLs, and calls to action. All 8 already-known real ads were
re-verified from the transcript and all 8 hold up: unsub-266 ×4
Progressive, unsub-287 Progressive pre-roll + LeafFilter ×2,
Acquired Sierra. The 2 house-plug-like blocks are Huberman
promoting his own book at pre-sale (neuroscience, 127:40–128:08,
"available by pre-sale at protocolzbook.com") and the Huberman
what-humans outro (120:52–121:59, follows, five-star reviews,
newsletter signup at HubermanLab.com). The 1 true false block is
SN 1102, 110:37–111:16: ordinary show content about a PHP
vulnerability disclosure; the "Shell.com" mention the block is
labeled for falls just after the span ends — the duplicate/URL
evidence came from just outside the block.

Across the 25 episodes the full pipeline marks 75 SB-matched ads
plus 43 further real paid ads the SB key cannot see (32 baseline +
5 rescue + 6 slot) — **118 real paid ads in total** — plus 3
self-promotional blocks and exactly 1 true false block, out of 122
blocks marked. The detector's "false positives" on this set are
almost entirely real ads SponsorBlock does not label, because SB
covers host-read description sponsors only. Per-show true false
blocks among the 35: SN 1102 has 1; every other show has 0.

Closest call, flagged for re-decision: block 15, the Huberman
what-humans outro. A generic outro could be read as an "outro
tail," but the span is dominated by explicit asks for the show's
own offerings (newsletter signup at HubermanLab.com is the
centerpiece), which is what a house plug is; it is classified
house-plug-like. Counting it as a true false block instead would
make the true count 2, not 1. The reasoning is spelled out in the
record. Records: `sb_eval/ab/fp_adjudication/`
(adjudication_35.md, adjudication_35.json, fp35_list.json,
exact_spans.json, spans.json, extract_spans.py, extract_exact.py,
build_adjudication.py). No transcript was missing for any episode —
zero blocks recorded as UNVERIFIED.

---

## 47. Slot determinism — PIN THE B DOWNLOAD + DRY-ISLAND GATE FIX, MERGED 2026-11-19

Slot output differed on every run of the same inputs (Ep 289:
gated slots 7 vs 8, standalone blocks 2 vs 3, the 5925s slot at
density 0.359 / 0.564 / absent, tail start 6159.8 → 6179.0 →
6161.1). Root cause measured, not assumed: the v5b aligner is
already deterministic — 3 runs on each of 13 frozen A/B pairs
(289 + 12 dynamic SB episodes) are byte-identical and match the
frozen align records exactly, and the 289 gate + merge replayed 3x
is byte-identical with merged blocks equal to the RUN B log. The
instability is input-side: B is re-downloaded fresh every run (ads
rotate) and re-transcribed every run (Whisper varies).

Held patch (`sb_eval/ab/slot_determinism/slot_determinism.patch`,
137 lines, `run_pipeline.py` only; NOT applied): (1) pin the B
download — keep `work/B/original.mp3` after alignment and reuse it
when `b_sha256.txt` records the same SHA, so re-runs align the
identical A/B pair; corrupt B / missing marker re-download fresh;
byte-identical-to-A B still deleted; siblings pruned to the 4
newest (5 pinned Bs per podcast max, ≈1.25 GB at ~250 MB per B;
289's B was 250,312,988 bytes). (3c) A-side gate ignores `reverb`
(dry-island) candidates — a slot whose only candidate overlap is a
single-vote dry island passes the gate on noise; exactly one such
slot exists across all 28 raw slots (289's 5926.4–5940.1), and the
B-side gate does not rescue it.

Measured on the frozen SB set (baseline reproduced exactly first,
`combined_repro38`): combination SB numbers IDENTICAL to baseline
on every headline — slot-flow 0.682 (73/107) / FP 42, full 0.701
(75/107) / FP 47, medians and within-1s flat. On the frozen 289
pair: 11 blocks (was 12); 23:38 kept as merged 1398.4–1443.3; 98:45
dropped; 102:41 kept as 6179.0–6228.6, still ~26s early — the start
overshoot is a separate edge problem, not fixed here. Pin test
(mocked download/transcription): 3 repeat runs byte-identical
slots, 0 re-downloads, 0 re-transcriptions; corrupt-B and
missing-marker both re-download; prune keeps 4 newest siblings.

DISCARDED: (2) deterministic anchor selection — the aligner is
already deterministic, nothing to fix. (3a) density floor 0.7 and
(3b) min diff-words ≥ 40 — both drop the 289 noise slot but also
the real LinkedIn ad (0.677 / 21 words vs noise 0.682 / 30 words:
no threshold separates them); SB FP 42 → 41 / 47 → 46 looks better
only because SB never labels dynamic ads. (3a) floor 0.6 — no-op,
identical to baseline everywhere.

Patch verified: dry-run + apply clean to a fresh live copy,
compiles, byte-identical to the tested files
(md5 `db41bdf01f5aaab83fa918a4f4bd46d4`, parent-verified by
independent dry-run + apply + compile on a fresh copy). Record:
`sb_eval/ab/slot_determinism/` (NOTE.md, patch, drivers, variant
results). No live files modified, nothing merged or published.

Corrections to the record: the variant NOTE.md's "29 raw slots"
is a count error — `gate_evidence.json` has 28 rows (parent
verified); the decisive finding (exactly one dry-island-only slot)
is unaffected. The 289 replay's detector stage does not match RUN
B's log-extracted detector list (7 vs 10 blocks) because the
extraction reads "refine" lines, which the three Dry-Island blocks
lack in RUN B's log — a logging/extraction artifact, not a run
difference; the merged output matches RUN B exactly. The frozen
pair's baseline differs from the final retry's state by design
(12 blocks, vsafe extended to 1443.3, 102:41 start 6179.0, vs the
final retry's 13 blocks, vsafe unextended plus a separate slot,
102:41 start 6161.1) — the frozen pair uses a different B download
than the final retry, which is precisely the instability being
fixed; the combination's 289 numbers are measured on the frozen
pair, not the final retry's B.

**MERGED 2026-11-19 (owner directed).** Backup
`tmp/run_pipeline_pre_slotdet_merge_2026-11-19.py`, md5-verified
identical to the pre-merge live file. Live run_pipeline.py
`8aa885fe…` (4,070 lines) → `db41bdf01f5aaab83fa918a4f4bd46d4`
(4,119 lines) — exactly the expected md5, byte-identical to the
tested copy; split_rescue.py untouched. Post-merge verification on
frozen inputs (drivers asserted module `__file__`, live
`pipeline/` first in `sys.path`): SB detector 0.701 (75/107) /
FP 40 / median 4.8s / mean 13.1s / within-1s 26.0% / within-2s
36.7% / max 79.3s; slot-flow 0.682 (73/107) / FP 42; full 0.701
(75/107) / FP 47 — every headline identical to the pre-merge
numbers. 286 7/9 ≥80%, 606.7/673s, 0 FP; 287 6/8, 463.0/531s, 1 FP
(Pepperbox). Synthetic 23/27 ≥50%, 19/27 ≥80%, FP 3, median
2.55s; TTS 9/9 ≥50%, 8/9 ≥80%, FP 0, median 1.32s; null 8 blocks
identical. Frozen 289 pair: 11 blocks; 23:38 kept 1398.4–1443.3;
98:45 dropped; 102:41 kept 6179.0–6228.6. Pin behaviour verified
on the merged live file: run 1 downloads 1×, transcribes 35
chunks, 6 slots; runs 2–3 download 0×, 0 new transcriptions
(35 cache hits), slots byte-identical; corrupt B and missing
marker each trigger exactly one fresh download with identical
slots. Records: `sb_eval/ab/slot_determinism/merge_verify/`.
Nothing published; no Ep 288/289 state modified. The 102:41
start overshoot remains — fixed by entry 49's held patch, not by
this merge.

---

## 48. Re-score against the adjudicated truth set — TRUE RECALL 0.787, TRUE FP 1 (2026-11-18)

The SB-keyed numbers understate the pipeline: SB labels cover
host-read description sponsors only, so 43 real ads the pipeline
finds have no label. The adjudicated truth set is 107 SB labels +
43 extras (32 baseline + 5 rescue + 6 slot) = 150 known real ads.
The full pipeline marks 122 blocks: 118 real paid ads, plus 3
self-promotional blocks, plus 1 true false block. **True recall
0.787 (118/150)** vs SB-keyed 0.701 (75/107). **True FP 1** (4
strict, counting the 3 self-promotional blocks as false).

Per-show (true ads / found / true recall / SB recall / true FP /
strict FP): 99pi 3/3/1.000/1.000/0/0; Acquired 9/5/0.556/0.000/0/0;
Huberman Lab 47/39/0.830/0.750/0/2; Lex Fridman 6/4/0.667/0.500/
0/0; Modern Wisdom 21/15/0.714/0.625/0/1; Security Now 34/28/
0.824/0.769/1/1; Unsubscribe 18/18/1.000/1.000/0/0; Vergecast
12/6/0.500/0.500/0/0. Totals: 150/118/0.787/0.701/1/4. Extras
breakdown closes: baseline 32 (Acquired 5, Huberman 8, Lex 1,
Modern Wisdom 3, Security Now 8, Unsubscribe 7) + rescue 5
(Huberman 4, Lex 1) + slot 6 (Huberman 3, Modern Wisdom 2,
Unsubscribe 1). Block composition closes: 118 real + 3 self-promo
+ 1 true false = 122.

Caveat, precisely: 150 is a lower bound on the true ad count and
0.787 is an upper bound on true recall — the 32 missed are SB
labels only, missed dynamic ads are unknown. The true FP of 1 is
exact for marked blocks (all 122 adjudicated). Edge stats are
SB-based only (median 4.8s, mean 13.1s, within-1s 26.0%,
within-2s 36.7%, max 79.3s), covering the 75 SB-matched ads; the
43 extras have no independent true edges, except unsub-287 where
owner ground truth shows slot-flow edges closer (Progressive end
−3.7s → 0.0s exact; LeafFilter −6.1s → +0.9s, −1.7s → +0.5s).
Record: `sb_eval/ab/adjudicated_score/` (NOTE.md,
adjudicated_score.json).

---

## 49. Slot start trim — REPEATED-PHRASE HEAD TRIM, MERGED 2026-11-19

On the frozen 289 pair the tail slot started at 6179.0, ~26s
before the Focker copy starts at 6205.3 in A. Root cause
measured, not assumed: the density start-extension accounts for
only 12.1s of it (9 tokens back from the first differing token
at 6191.0); the other 14.2s sits inside the slot, because A
repeats the outro chant "You won the quarter" ~15 times while B
repeats it ~3 times then plays its own ad — the extra chant
repetitions are genuine A-vs-B differing tokens, so density
alone cannot tell chant from ad copy.

Held patch (`sb_eval/ab/slot_start_trim/slot_start_trim.patch`,
54 lines, `pipeline/slot_align.py` only; NOT applied): in
`density_slots`, after the existing span is computed, if the
slot head is ≥3 consecutive repeats of one 4-token phrase,
advance the start past the last full repeat (remaining slot must
stay ≥ 8s). `diff_words` is recomputed over the trimmed span
only when the trim fires; otherwise the live counts are
preserved exactly. Generic: no names, times, or episode checks.

Measured (baseline reproduced exactly first, fresh
`fp_trim_base/`): on 289 the tail moves 6179.0 → 6204.9–6228.6
(+25.1s, 0.4s before the 6205.3 copy start — inside the 1s
goal); 23:38 kept as merged 1398.4–1443.3; 98:45 dropped;
11 blocks; no other 289 slot moves. On the 25-episode SB set
the numbers are IDENTICAL to baseline on every headline —
slot-flow 0.682 (73/107) / FP 42, full 0.701 (75/107) / FP 47,
medians and within-1s flat — and raw slots are identical on all
25 episodes (the trim fires nowhere else). Synthetic/TTS have no
A/B pair and never import `slot_align`, so no slot regression is
possible there.

DISCARDED: C1 trim-to-first-differing-token — tail only reaches
6191.0 (still 14.3s early), splits the 23:38 merged block into
detector + standalone slot, and drops the real LinkedIn slot
(7.9s < the 8s minimum). C2 (same trim, but `diff_words`
recomputed for every slot) — the counting change inflated every
slot's density and let the Mitochondria outro tail (0.176 →
0.824) pass the 0.5 floor, +1 SB FP; superseded by C2b, which
keeps the live counts unless the trim fires. C3
extension-density requirement and C4 ad-evidence head trim were
discarded on evidence without a run (no threshold separates the
tail extension, 5/9 differing, from real ads; C4 duplicates C2b
with more machinery and a layering violation).

Patch verified: dry-run + apply clean to a fresh live copy,
compiles, byte-identical to the tested file
(md5 `222b92a535251787ae86d4fa24e95083`, parent-verified by
independent dry-run + apply + compile on a fresh copy). Record:
`sb_eval/ab/slot_start_trim/` (NOTE.md, patch, drivers, variant
results). No live files modified, nothing merged or published.
Caveat: the `tracked` field in `variant_trim_c2b.json` shows
102:41 as empty only because the tracker looks near the old
start 6179.0; the block is present as `[6204.9, 6228.6, None,
'slot']` on both paths.

**MERGED 2026-11-19 (owner directed).** Backup
`tmp/slot_align_pre_starttrim_merge_2026-11-19.py`, md5-verified
identical to the pre-merge live file. Live slot_align.py
`8b708f88…` (325 lines) → `222b92a535251787ae86d4fa24e95083`
(360 lines) — exactly the expected md5, diff-clean against the
tested copy; run_pipeline.py and split_rescue.py unchanged.
Post-merge verification on frozen inputs (baseline gate
reproduced exactly first; drivers asserted module `__file__`,
live `pipeline/` first in `sys.path`): SB detector 0.701
(75/107) / FP 40 / median 4.8s / mean 13.1s / within-1s 26.0% /
within-2s 36.7% / max 79.3s; slot-flow 0.682 (73/107) / FP 42;
full 0.701 (75/107) / FP 47 — every headline identical to the
pre-merge numbers. 286 7/9 ≥80%, 606.7/673s, 0 FP; 287 6/8,
463.0/531s, 1 FP (Pepperbox). Frozen 289 pair: 11 blocks;
102:41 at 6204.9–6228.6; 23:38 kept as merged 1398.4–1443.3;
98:45 dropped; raw slots identical on all 25 episodes. Records:
`sb_eval/ab/slot_start_trim/merge_verify/`. One correction
during verification: the first detector-path scorer returned the
detect-only headline (0.682/73/FP 35) because it scored raw
`blocks_det`; corrected to detector + rescue from the frozen
merge inputs, it gives the expected 0.701/75/FP 40 exactly.

**Ep 289 re-run with the merged pipeline — DONE 2026-11-19,
PUBLISH GATE FAILED, nothing published.** The re-run (18:41–
19:14 CST, ~33 min, pipeline's own retry path, 289 guid only)
completed with no crash, A transcript reused from cache, B
downloaded fresh — but Megaphone served a truncated B:
220,200,960 bytes, 5,496.8s (91:37) vs A 249,504,239 bytes,
6,229.4s (103:49); B ends mid-sentence at 5,496.8 ("And he's
like,") while A continues, and B starts inside the Carvana
pre-roll. |B−A|/A = 11.76%, over the 10% `duration_ratio`
guard, so the slot flow rejected all slots
(`slots_rejected`). `download()` does no content-length check,
so the short read looked like success — the duration guard is
what caught it. The re-run produced 10 detector-only blocks
(Carvana 0:00, Poncho 15:20, Vsafe 23:18, Stop Box 31:40,
Dry-Island 38:05, Take Ultra 47:27, Dry-Island 55:25, FUM
61:00, Dry-Island 70:25, The Perfect Jean 74:43); only Poncho's
end differs from the published file (965.3 vs 961.5). The 5
gated slots (0.0–30.0, 1417.1–1443.3, 2298.8–2327.1,
3329.9–3368.0, 4224.4–4253.3) are all real detector-evidence
slots that would only have adjusted detector edges, but they
were never merged because the guard rejected the whole set; the
giant rejected slot 5499.7–6228.6 (728.9s, `too_long`) is the
missing-tail artifact of the truncated B, not an ad; the 98:45
and 102:41 regions have no B counterpart at all in this
download. The publish gate failed as designed — the re-publish
driver was written and verified but not run, and no hand-picking
was done. Published state unchanged (parent-verified live:
release 445857775 still carries the 10-block asset,
249,754,901 bytes, + suspects reel 12,409,241 bytes; served
feed blob sha unchanged). State: 289 now holds the re-run
(`slots_rejected`, 10 blocks, local marked file 249,754,856
bytes; new B pinned, sha 75067c51…); the state-repair landmine
fired as expected — `process_episode` dropped the four publish
fields, restored by hand to match published reality
(enclosure_bytes 249754901, suspects_bytes 12409241,
parent-verified). Ep 288 untouched (state sha identical,
history still 12 rows, feed enclosure still the original
Megaphone URL). History untouched (289 still 10 rows; no hand
edits). Log: `tmp/retry289_rerun_2026-11-19.log`. The blocker is
the truncated B, not the pipeline: a plain re-run without
deleting the pin would reuse the truncated B and reproduce
`slots_rejected` identically, so the next step needs the pinned
B deleted first (or a later re-download).

**SECOND RE-RUN + REPUBLISH — DONE 2026-11-19 (pinned B deleted,
fresh B, publish gate passed).** The pinned truncated B and its
marker were both deleted and verified gone; the re-run
(`tmp/retry289_final.py`, 289 guid only, ~15 min, exit 0, log
`tmp/retry289_rerun2_2026-11-19.log`) downloaded a fresh B:
245,506,466 bytes, sha c48dc606…, 6,129.5s vs A 6,229.4s (ratio
0.016 vs the 0.10 guard — a legitimate rotation difference, not
truncation). Alignment: 12,011 anchors, differing-token rate
0.1438, no guards tripped, `dynamic_status=slots`. Block list
(11): Carvana 0:00–0:30 [merged]; Poncho 15:20–16:05
[detector]; Vsafe 23:18–24:03 [merged]; Stop Box 31:40–32:15
[detector]; Ad 38:19–38:47 [merged]; Take Ultra 47:28–49:31
[detector]; Ad 55:30–56:08 [merged]; FUM 61:00–62:25
[detector]; Ad 70:24–70:53 [merged]; The Perfect Jean
74:43–76:40 [detector]; Ad 103:25–103:48 [slot]. Publish gate
passed: 11 blocks; 23:38 merged into Vsafe (1398.4–1443.3);
98:45 absent (5928.6–5940.1 rejected `no_ad_evidence`, density
0.083); the 102:41 slot starts at 6205.2, within 0.1s of the
6205.3 copy start; the only standalone new slot is 6205.2. The
38:19, 55:30, 70:24 blocks carry slot edges replacing the old
detector edges — designed gated-slot behavior, the same slot
edges as the previous run, not new slots. Publish
(`tmp/republish289.py`, exit 0): release 445857775 now carries
the new marked asset, 249,754,974 bytes (was 249,754,901),
md5 `405ff6453107d077bde5db7d4dab73a4`, byte-identical to the
local marked file; suspects reel 12,409,241 bytes (unchanged);
served feed 289 enclosure points at the new asset, length
249754974 (the old length 249754901 appears 0 times in the
served feed); ffprobe on the published asset: 11 ad chapters,
6205.2–6228.6 "Muse Detected Ad" present, no chapter near
98:45. State publish fields: the landmine fired as expected
(all four fields None after the re-run); the republish driver
set them from the upload responses and saved state — no hand
repair needed (enclosure_bytes 249754974, suspects_bytes
12409241, parent-verified). The content-length check was live
during the run but did not fire — the B download completed
cleanly on the first attempt, no retries. Ep 288 untouched
(state 12 blocks, no enclosure key, served feed still the
Megaphone original). History untouched (289 still 10 rows, 288
still 12 rows; no hand edits).

**PARENT FINDING — stale B transcript cache in the second
re-run (2026-11-19, found during verification).** The second
re-run's B transcript reused chunks 000–030 from the truncated
B: their file timestamps are 12:45–13:11 (the first re-run),
while only chunks 031–034 were written fresh (15:20–15:22) and
transcript.json was rebuilt at 15:22. Cause, read in the code:
`_b_cache_check` drops stale chunks only when the marker file
exists AND its recorded SHA mismatches the new B's SHA; when
the marker is missing it writes the new SHA and returns without
dropping anything. The parent's brief told the coordinator to
delete BOTH the mp3 and the marker — deleting the marker is
what defeated the invalidation. (Deleting only the mp3 and
keeping the marker would have triggered the drop, since the old
SHA would mismatch.) The 102:41 slot sits in the
freshly-transcribed tail (chunk 034 covers 6120–6300s) and is
correct for the fresh B. The four early merged slots' edges
(23:38, 38:19, 55:30, 70:24) were computed against the
truncated B's transcript, not the fresh B's — their correctness
for the fresh B is UNVERIFIED. A targeted check of the fresh B's
audio at those regions confirms the two downloads differ there
(the fresh B plays show banter where the truncated B's
transcript has ad copy), so the edges cannot be assumed right.
The fix is to make `_b_cache_check` drop the chunks when the
marker is missing too; then a clean re-run and a re-publish if
the blocks differ. Open item — not yet designed or tested.

**THIRD RE-RUN (clean) — STOP, NO RE-PUBLISH (2026-11-19).**
The B side worked as designed: same pinned B throughout (sha
c48dc606…, 245,506,466 bytes, verified before/after; log
"reusing pinned B download", no re-download); all 35 B chunks
freshly transcribed 22:31–22:57 UTC, transcript.json rebuilt.
Deletion set was chunk_*.json + transcript.json by hand; marker
+ mp3 kept (the probe code deletes + re-downloads the B if the
marker is missing). BUT the experiment was confounded by the A
rotating — and the present-destination HEAD check (entry 52,
just applied) caused it: at 22:26:58 the HEAD check fired
(local A 249,504,239 bytes vs server-advertised 243,897,322),
deleted the good cached A, and downloaded a rotated cut
(243,086,482 bytes, sha 09769e29…, 6,068.98s vs old 6,229.4s).
A HEAD after the run advertised 250,072,662 — a third length.
Megaphone rotates the A on nearly every request, so a length
mismatch does NOT mean the local file is truncated. Worse,
`transcribe_chunked` has NO audio-identity check on the A
cache (same bug class as the B bug just fixed): it reused the
Nov 15 A chunks 000–033 and dropped chunk 034 entirely (244
words, 6,120–6,228.6s) because its start exceeds the new
duration. Transcript went 19,074 → 18,830 words. Detection ran
on old-A words + new-A audio — a chimera. Clean blocks (13) vs
published 11: the two new blocks are false (40:25 show banter;
58:45 comedy bit — the previous run rejected the same span
twice); the 98:49 slot is false show banter (previous run
rejected that exact span twice; violates "98:45 absent"); the
102:41 slot is real Fockers ad copy, lost only because the new
A is shorter than the slot position; FUM start/end and Perfect
Jean start favour published. Clean is better on only 2 edges
(Vsafe start 1417.0; Perfect Jean end 4590.0). Verdict: clean
set worse — do not publish. `tmp/republish289.py` was not run.
Verification: release 445857775 unchanged (marked 249,754,974,
suspects 12,409,241); fresh post-run download md5
405ff645… identical to pre-run; served feed byte-identical
pre/post (md5 05f337a3…), 289 enclosure length 249754974, 288
still Megaphone; state publish fields — landmine fired,
restored by hand and verified; Ep 288 untouched; history
untouched. Log: `tmp/retry289_clean_2026-11-19.log` ("289
after: dynamic_status=slots slots=7 rejected=1 blocks=13").
Open items from this run: (1) the HEAD check is unsafe for
rotating sources — REVERTED by the parent the same day (entry
52); a scoped redesign is a possible separate task. (2) The A
transcript cache needs the same SHA-based invalidation as the B
cache — a new fix task, same pattern as entry 51. (3) Residual
state mismatch (not hand-edited): state 289 now says blocks=13,
bytes 243330993 (the confounded run) while the publish fields
point at the 11-block old-A file — a future feed regeneration
from this state would list 13 ads against the 11-block
enclosure; needs a decision on restoring the state's blocks to
the published 11. (4) The old-A original.mp3 (249,504,239
bytes) no longer exists on disk; the published marked mp3 is
the surviving copy of that audio. The four early merged slots
remain unverified for the fresh B in the strict sense — this
run's slot flow used truncated old-A words. A true verification
needs the old A audio (recoverable from the published marked
file) or a fresh pin of the current A with its own transcript.

---

## 50. Content-Length check on download() — APPLIED 2026-11-19

The defect: `download()` performed no content-length check, so a
short read looked like success. This bit us on 2026-11-19:
Megaphone served a truncated B for Unsubscribe Ep 289 —
220,200,960 bytes, 5,496.8s vs A 249,504,239 bytes, 6,229.4s; B
ends mid-sentence ("And he's like,") — and only the duration
guard (10% duration_ratio) caught it. The download step must
fail loudly instead.

Frozen design (written before any test run, tested exactly as
written): inside `download()`, count bytes written during the
read loop, then read the Content-Length header from the response
(`r.getheader("Content-Length")`, falling back to
`r.headers.get(...)`). If the header is present and parses as an
integer, compare bytes written to it; on ANY mismatch
(`written != expected`) raise `IOError("download truncated:
...")` inside the existing `try`, so the existing cleanup
(partial file deleted) + retry + loud re-raise engage. If the
header is absent (e.g. chunked encoding) or unparseable, skip
the check (fail open). Nothing else changes. Generic: no
hardcoded sizes, names, or episode checks. Callers verified:
`process_episode` fails loudly; both B flows degrade to
detector-only.

Tests (a)–(e), all PASS: (a) truncated reproduction 12/12 —
mocked response (CL 10,240, clean EOF after 4,000 bytes) and a
real localhost server closing early both raise, dest file
removed, retried (2 urlopen calls with retries=2);
CL-smaller-than-body also raises and removes dest. Control on
pre-change live code: 5/12 — truncated downloads silently
"saved 4000 bytes" (no IncompleteRead raised), proving the test
reproduces the defect. (b) Complete download: identical bytes
(10,240), one urlopen call. (c) Missing Content-Length:
identical bytes; unparseable header ("garbage") also passes
(fail open); missing header + short body passes by design. (d)
SB set: baseline reproduced exactly first (fresh
`combined_repro_clc`) — detector 0.701 (75/107) / FP 40 /
median 4.8s / within-1s 26.0%; slot-flow 0.682 (73/107) / FP 42;
full 0.701 (75/107) / FP 47; patched copy and live-after-apply
identical on all four headline dicts (incl. detect-only 0.682 /
FP 35) — zero detection impact. (e) Pin-B path 9/9: run 1
downloads 1x, transcribes 35 chunks, 6 slots; runs 2–3 download
0x, transcribe 0, 35 cache hits, identical slots; corrupt B /
missing marker each re-download 1x with identical slots; prune
keeps 4 newest siblings; byte-identical B still deleted
(no_slots); truncated B served → 3 attempts, status b_failed,
detector-only blocks, no file left behind; complete B after →
identical slots.

Applied (owner directed as an addition, test-first): backup
`sb_eval/ab/content_length_check/run_pipeline.py.pre-clc.bak`,
md5-verified identical to pre-change live. New live
`pipeline/run_pipeline.py`: md5
`bfd809200d5aaba9fd269bc570e41749`, 4,145 lines (+26);
`py_compile` clean; diff live vs tested file empty.
`split_rescue.py` and `slot_align.py` untouched. Live during
the 289 second re-run; did not fire — the fresh B download
completed cleanly on the first attempt. Record:
`sb_eval/ab/content_length_check/` (NOTE.md, drivers, results
JSONs). Caveats: the check only catches truncation when the
server advertises a Content-Length larger than what it sends —
if a server advertises a CL matching its truncated body, or
serves chunked with a clean termination, no length check can
catch it; the duration guard remains the backstop. The early
return in `download()` for an already-present non-empty dest is
unchanged; a pre-existing truncated file is still trusted on
re-entry (the pin path checks SHA itself, but
`process_episode`'s A download does not) — a possible separate
task.

---

## 51. `_b_cache_check` fix (missing marker drops stale chunks) — APPLIED 2026-11-19

The defect: `_b_cache_check` dropped stale B chunks only when
the marker file existed AND its recorded SHA mismatched the new
B's SHA; when the marker was missing it wrote the new SHA and
returned without dropping anything. This is what bit the 289
second re-run (entry 49): deleting the marker defeated the
invalidation and the re-run reused chunks 000–030 from the
truncated B.

Frozen design (written before any test run, tested exactly as
written): dedent the chunk/`transcript.json` drop out of the
`if os.path.exists(marker)` block, so it runs on every
non-early-return path — a missing marker is treated exactly
like a SHA mismatch (new log line: "no B transcript marker;
dropping any stale B chunks"). Matching marker still returns
early (cache valid, no re-transcription); mismatching marker
still drops; pin-B reuse untouched. One function, +4 lines.
Generic, no hardcoded sizes, names, or episode checks.

Tests (a)–(e), all PASS: (a) missing marker + stale 3-chunk
cache — live control KEPT the stale chunks (0
re-transcriptions, stale words returned), patched DROPPED them
and fully re-transcribed (3/3, zero stale words); (b) matching
marker → cache kept, 0 re-transcriptions; (c) mismatching
marker → drop, full re-transcription; missing marker + empty B
dir → no crash; (d) SB set identical on all four headlines
(detector 0.701/40, slot-flow 0.682/42, full 0.701/47,
detect-only 0.682/35); (e) pin-B path — patched missing-marker
case re-downloads 1× AND fully re-transcribes all 35 chunks (0
cache hits) with identical slots (hash 13a57ae01f0a48ba).

Applied: intermediate live md5
`129f4e349a08d94f9163f2b855998750`, 4,149 lines;
`py_compile` clean. (Later combined with the present-dest
check into md5 ccd70395…, 4,193 lines; the present-dest check
was then REVERTED — entry 52 — restoring this file as live.)
Record: `sb_eval/ab/b_cache_check_fix/` (NOTE.md, patch,
drivers, results JSONs, `both_combined_results.json`).

---

## 52. Present-destination download check — APPLIED then REVERTED 2026-11-19

The defect: the early return in `download()` for an
already-present non-empty dest trusted the file — a
pre-existing truncated file was trusted on re-entry (the pin
path checks SHA itself, but `process_episode`'s A download
does not).

Frozen design (written before any test run, tested exactly as
written): new helper `_head_content_length(url)` issues a HEAD
request (same User-Agent, 30s timeout), returning `None` when
the length cannot be determined (absent/unparseable header, any
request failure incl. 405 — fail open). In the early-return
branch, if it returns a length and the local size differs, the
local file is deleted and the normal download loop runs;
otherwise the file is trusted as before. Fresh downloads issue
no HEAD. `download()` + helper only, +44 lines. Generic, no
hardcoded sizes, names, or episode checks.

Tests (a)–(f), all PASS on the test rig: (a) pre-existing
4,000-byte file + server advertising 10,240 — live control
trusted it silently (0 HEADs, 0 GETs), patched deleted and
re-downloaded complete (1 HEAD, 1 GET), mocked and real
localhost server; (b) complete + matching CL → 1 HEAD, 0 GETs;
reverse mismatch also re-downloads; (c) no/unparseable CL →
trusted, 0 GETs; (d) HEAD 405 and connection error → trusted,
no crash, 0 GETs; (e) SB set identical on all four headlines;
(f) A-download call path verified by mock.

Applied (combined live md5 ccd70395…, 4,193 lines; combined SB
re-verification identical on all four headlines). THEN REVERTED
the same day by the parent: in the clean 289 re-run (entry 49),
the HEAD check fired on the cached A (local 249,504,239 bytes
vs server-advertised 243,897,322), deleted the good cached A,
and downloaded a rotated cut (243,086,482 bytes, sha
09769e29…, 6,068.98s vs old 6,229.4s). A HEAD after the run
advertised a third length (250,072,662) — Megaphone rotates
the A on nearly every request, so a length mismatch does NOT
mean the local file is truncated. The re-run was confounded
(detection ran on old-A words + new-A audio) and produced 13
blocks, 3 of them false — the publish was correctly refused.
The check is unsafe for rotating sources: it treats rotation
as truncation. Revert: reverse-applied
`present_dest_check.patch`; live restored to md5 129f4e34…,
4,149 lines (the `_b_cache_check` fix only); `py_compile`
clean; cache fix still present; HEAD helper gone. The patch is
preserved at `sb_eval/ab/present_dest_check/
present_dest_check.patch` for a scoped redesign (e.g. only fire
when the local file is smaller than advertised, or only for
non-rotating hosts). Record:
`sb_eval/ab/present_dest_check/` (NOTE.md, patch, drivers,
results JSONs).

---

## 53. Full pipeline audit — DONE 2026-11-19 (analysis only, no fixes)

Owner: "do a full analysis of the pipeline looking for bugs — that
should be faster than just retrying again and again." Three teams
read the pipeline end to end (run_pipeline.py all 4,149 lines,
slot_align.py, split_rescue.py, transcript_diff.py,
xep_recurrence.py, podcast_history.py, publish_feed.py,
publish_github.py, regen_feed.py, tmp/republish289.py,
tmp/retry289_final.py, gh.py, tmp/pin_unsub_upload.py, plus live
state/history/feed); the coordinator verified every finding
against the code or live data. Result: **3 CRITICAL, 37 MAJOR,
21 MINOR — 61 consolidated findings** (66 raw minus 5
duplicates merged). Full record:
`sb_eval/ab/audit_2026-11-19/FINDINGS.md` (498 lines), per-team
files under team1/, team2/, team3/. Parent verified all three
CRITICAL findings against the live code personally.

CRITICAL: **C1** — publish saves state BEFORE the feed push
(`publish_github.py:86–109`): release created, asset uploaded,
`enclosure_url`/`enclosure_bytes` written and `save_state()`
called, then the feed is regenerated and pushed; if the feed
push fails, the episode is never selected again (selection is
episodes *without* `enclosure_url`), and `regen_feed.py` never
pushes — listeners keep getting unmarked audio for an episode
the operator believes is published. **C2** — edge refinement
can shrink a block far below the floors with no re-check
(`detect_ad_blocks` ~1660–1700): after the mention-span cap,
each edge moves inward to the nearest method-segment boundary
and `refined.append((s, e, l))` runs with no duration test;
mocked repro refined a 67 s heat-map block to 100.0–106.5 s
(6.5 s) and returned it as a block — the 30 s / 45 s floors are
void after this step. **C3** — one slot overlapping two
detector blocks collapses both into a single block
(`_merge_slots` 3425–3450): all overlapped blocks are marked
`used` and replaced by one merged block `(slot start, slot end,
one label)`; mocked repro — BrandA 90–150 s and BrandB 150–210
s, slot 100–200 s → one block 100–200 s labelled `slot:BrandA`,
BrandB gone entirely, and `split_rescue` cannot recover it.

MAJOR (37), grouped: download/pinning/caching/transcription
(6) — M1 early return trusts any non-empty file (unmitigated
after the entry-52 revert); M2 a zero/near-zero transcript
publishes silently as "no ads"; M3 stale out-of-range chunk
files never pruned (live 289 A dir: 35 chunk files, 19,074
words vs transcript.json 18,830 words — stale chunk_034, 244
words); M4 entry work runs before any cache check; M5
cross-episode span cache keyed on mtime + size only; M6 a B-flow
retry reprocesses the whole episode against whatever A is on
disk now, and live `base_url` is `''` for both podcasts, so a
feed regen after such a retry silently points back at unmarked
publisher audio. Detection/slot flow/chapters (19) — M7 density
floor bypassed on slot-detector overlap; M8 partial-overlap
merge has no minimum (a 5% touch absorbs a whole slot); M9
slot merges can produce overlapping published blocks; M10/M11
brand and orphan methods return unbounded segments (863 s and
690 s in repro); M12 transcript-diff cache keyed only on URL +
age; M13/M14 missed-sponsor check skips short-variant sponsors
and uses a substring "covered" test; M15 TLD lists out of sync
again (`find_url_positions` lacks `us`, `agency`, `digital`,
`solutions`, `systems`, `network`, `security`); M16
`find_mentions` multi-token variants match by substring per
word; M17 slot A-side gate passes on any overlap > 0 incl.
voice-only evidence; M18 slot B-side gate requires brand + URL
in the same window but not near each other; M19 slot density
uses a mismatched numerator/denominator (reported 0.767, true
0.930 in repro); M20 voice/reverb audio failures propagate and
fail the episode; M21 `_method_chapter` crashes on an empty
transcript when chapters exist; M22 `_merge_chapters` drops an
RSS ad chapter starting within 2 s of an ID3 chapter; M23
URL/orphan/chapter pause-snapping uses the wrong side of the
silence; M24 URL grouping chains transitively (one extra URL
can delete a whole group); M25 post-detection extensions never
re-check `MAX_AD_LEN`. State/history/publishing/errors (12) —
M26 `main()` returns early with no new episodes, so a failed
feed regen is never retried; M27 `publish_feed.py` can never
run (no `init_podcast()`, wrong `STATE_PATH`); M28 concurrent
`save_state()` writers share one fixed `.tmp` path (19/40 saves
raised `FileNotFoundError` in a 20-round test); M29
`load_state()` has no error handling and no backup; M30 history
first-write-wins keeps stale blocks (live 289: history 10 rows
from 2026-11-15 vs state 13 blocks); M31 history
check-then-append is not atomic; M32 `write_feed()` crashes
with `KeyError: 'bytes'`; M33 `write_feed()` lets a pinned
original silently shadow a marked episode; M34 dynamic-flag
state is mutated in memory and only persisted at the end, and a
crashing episode retries forever with no cap; M35 `fetch_feed()`
curl fallback returns any non-empty response with no
validation; M36 `publish_github.py` creates a release with no
existing-tag check; M37 `republish289.py` deletes the old
suspects asset then leaves state pointing at it when the local
file is missing.

MINOR (21) — m1 `_b_cache_check` swallows chunk-delete
failures then writes the marker anyway; m2 `fetch_transcript`
is a single attempt with no truncation check; m3 a pinned B can
entrench a truncation the length check cannot see; m4 model
cache has no version identity, `MODEL_DIR` is dead; m5 no
timeouts anywhere on the transcribe path; m6 `transcribe_chunked`
leaves `chunk_*.wav` files behind on exception; m7
`_prune_pinned_b` deletes the B audio but keeps its transcript
cache and marker; m8 B-transcript readers outside the live path
bypass `_b_cache_check`; m9 duplicate detection misses a repeat
occurring only in the last 8 words; m10 a slot overlapping a
house-plug block is dropped even when it also overlaps a real
ad block; m11 `insert_ad_chapters` "contiguous" check is always
true; m12 `_part` None-guard exists only in `split_rescue`;
m13 detection assumes word timestamps are monotonic (UNCERTAIN,
no repro); m14 slot misnaming on short popular-brand variants
(UNCERTAIN, no repro); m15 `publish_github.py` treats ANY `gh()`
failure on the feed GET as "feed missing"; m16 one missing audio
file blocks the whole publish queue, and `publish_feed.py`
re-publishes the newest episode on every run; m17 silent failure
modes (pending-retry skip, missing suspects reel, silent
transcript-diff skip); m18 fixed shared temp/payload paths are
a cross-process race; m19 corrupt transcript-chunk cache
crashes the episode instead of re-transcribing; m20 history
dedup key is inconsistent across writers; m21 `download()`
retries non-retryable errors 6 times with long sleeps (404s
included).

Confirmed known issues: truncated-B check fixed; B-cache fix
fixed; HEAD check correctly reverted; A transcript cache still
OPEN (with new sub-issue M3); state landmine OPEN and wider
than known (rewrite also drops all seven `pinned_original_*`
fields plus `skipped`/`reason` and legacy `ads_*` fields).
Completeness answers: `_b_cache_check` covers every LIVE
B-transcript reader (only `transcribe_chunked` from
`run_slot_flow`); content-length check covers every FRESH
download but not the early return (M1) or pinned-B reuse (m3);
two more caches share the A identity gap — transcript-diff
cache (M12) and xep span cache (M5). Not finished: Team 1 did
not finish detection-method internals and
`publish_github.py`/`publish_feed.py` (grep-verified only);
Team 3 did not finish the detection core (~lines 666–3070) and
`tmp/retry289_final.py`; the frozen `dynamic_slots/align.py`
prototype and `sb_eval/ab/*` harness scripts were background
only. No fixes were made — analysis only; no pipeline or live
file was modified; no episode was processed; no downloads were
made.

---

## 54. Fix round for all 61 audit findings — APPLIED 2026-11-19/20

Owner: "write and apply fixes for all 61 findings, then run the
audit again." Three fix teams (same subsystem split as the
audit) fixed every finding, test-first, applied only if clean.
Result: **60 FIXED, 1 INVALID as proposed, 0 held, 0
unproven.** Both uncertain findings (m13, m14) were confirmed
by reproduction and fixed. No live episode was processed,
nothing was re-published. Records:
`sb_eval/ab/fix_round_2026-11-19/` (team NOTEs with frozen
designs, test results, patch lists, SB before/after; patches in
team1/patches/ (5), team2/patches/ (11), team3/*.patch (5);
consolidated SB results in
`team2/results/sb_coord_consolidated.json`).

Consolidated SB results (coordinator's own run on the fully
patched tree, fresh variant dirs): detector **0.757 (81/107) /
FP 37 / median 4.8s / within-1s 25.9%** (was 0.701 (75/107) /
FP 40 / median 4.8s / within-1s 26.0%); detect-only **0.738
(79/107) / FP 31** (was 0.682 (73/107) / FP 35); slot-flow
**0.738 (79/107) / FP 38** (was 0.682 (73/107) / FP 42);
full **0.757 (81/107) / FP 44** (was 0.701 (75/107) / FP 47).
Recall +6 matched on every path, FP −3 to −4, edge accuracy
essentially unchanged. Every change is explained by four fixes,
verified per-episode: M11 v3 (orphan window now the densest
mention cluster, one segment per sponsor — SN-1096 5/6→6/6,
SN-1097 4/6→5/6, SN-1099 4/5→5/5), m9 (duplicate off-by-one —
willpower 2/4 FP 2→4/4 FP 0), M15 (TLD sync — 3 extra `material`
URL anchors on SN-1096), M24 (URL group split — one new `face`
FP on SN-1096 that replaces the base joindeleteme FP, net
SN-1096 FP unchanged). Two new single FPs from M11 v3, both
explained: sydney-sweeney `Find William` (content mentions are
the densest cluster) and unsub-287 `RIDGE` (verified
"Rattlesnake Ridge" conversation, not a Ridge read).

Critical — all FIXED: **C1** (publish now: release → upload →
checkpoint-1 state with `feed_pushed: False` → regen feed →
push feed → re-GET and confirm enclosure, checkpoint 2;
selection includes `feed_pushed is False` /
`republish_needed`); **C2** (refined block < 30s restores
pre-refinement edges); **C3** (one slot over two different-ad
blocks keeps both detector blocks; same-ad collapse preserved).

Team 1 (download/caching/transcription) — all 16 FIXED: M1
(sidecar `<dest>.download.json` with bytes + SHA-256 + ffprobe
duration; no live HEAD/length check anywhere, entry-52 rule
honoured); M2 (raise on probe duration ≤ 0; plausibility check
< 20 words/min); M3 (out-of-range chunks pruned; glob readers
duration-filtered); M4 (complete-cache fast path, lazy model
load); M5 (xep cache key = content SHA-256 + version); m1
(survivor check raises, marker not written); m2
(fetch_transcript retries + curl fallback + length validation);
m3 (pin re-validated against sidecar duration + A ±10%); m4
(model revision recorded in chunks; MODEL_DIR deleted; model
cached per-process); m5 (timeouts on all transcribe-path
subprocess calls); m6 (per-chunk try/finally wav removal +
entry sweep); m7 (prune removes B cache + marker + sidecar);
m8 (`transcribe_b()` helper; all B transcription routed
through it); m19 (corrupt chunk JSON treated as cache miss;
atomic chunk/transcript writes); m21 (4xx except 408/429 fail
fast; capped backoff; curl fallback). **(a) A transcript cache
— FIXED:** `a_identity.json` sidecar (SHA-256 + size +
duration); mismatch or missing identity drops all A chunks +
transcript.json. Team 1 SB: identical on all four paths.

Team 2 (detection/slot flow/chapters) — 26 FIXED, 1 INVALID:
M7 (density floor on every merge path), M8 (partial merge needs
≥10s AND ≥10% of slot), M9 (final pass clips overlaps, drops
blocks clipped below 5s), M10 (MAX_AD_LEN cap in
`_expand_segment`), M11 (v3 densest cluster, one segment per
sponsor — v1 and v2 measured and rejected, results preserved),
M12 (transcript-diff cache gains `whisper_sha256`), M13
(dropped the `len>=4` variant filter), M14 (label-aware
coverage on both tests), M15 (shared 29-entry `URL_TLDS`;
split_rescue synced), M16 (exact token equality +
plural/possessive, not `_boundary_match` — deviation recorded,
because the boundary tolerance would still match "minty
mobile"), M17 (A-side gate: cta/cluster/brand/chapter only,
overlap ≥5s AND ≥10%), M18 (B-side gate: brand and URL within
60s), **M19 INVALID as proposed** (re-measured all 18 frozen
gated slots: live false 0.176 vs lowest true 0.677 — the
"mismatch" is the calibrated discriminator; audit options A/B
both destroy it; comment-only docstring patch applied, no
behaviour change), M20 (try/except around ffmpeg decode,
returns []), M21 (empty-transcript guard; final-chapter end uses
last word's END), M22 (ad-title-aware chapter dedupe), M23
(pause-snapping corrected to start→pe, end→ps), M24 (URL groups
split when span > 125s), M25 (final MAX validation after all
extensions), m9 (range off-by-one), m10 (plug veto per-block),
m11 (contiguity from last KEPT chapter), m12 (`_part` guards
incl. split_rescue), m13 (CONFIRMED — disordered words missed
a late ad entirely; `_sorted_words` at top of
detect_ad_blocks and end of transcribe_chunked), m14
(CONFIRMED — 3 content uses of "seed"/"mint"/"factor" misnamed
slots; popular-brand pool now requires a bare-URL/CTA anchor).

Team 3 (state/history/publishing/errors) — all 19 FIXED: M6
(`_retry_dynamic_only()` reuses on-disk A transcript and
stored blocks, 0 A calls; `_build_state_entry()` preserves
publish/legacy fields), M26 (write_feed runs unconditionally
at end of every run), M27 (publish_feed.py calls
`init_podcast()` with `--podcast`), M28 (unique temp names +
fsync + re-entrant state lock; 0 errors in 20 rounds × 2
threads vs audit's 19/40), M29 (load_state falls back to newest
parseable `state.json.bak-*`; save_state rotates 5 backups with
fsync), M30 (re-process replaces the episode's rows), M31
(per-file flock across load→dedup→rewrite), M32 (eager-default
KeyError fixed; missing length → 0 with warning), M33 (marked
entry wins over pinned for same guid), M34 (dynamic
flags/attempts saved immediately; crash cap 3 with persisted
per-guid counter), M35 (`_validate_feed_xml()` on both fetch
paths), M36 (GET-by-tag before POST; reuse on 200), M37 (local
suspects file verified before any delete; missing file clears
suspects fields with ERROR log), m15 (`GhError(status,
message)` replaces `sys.exit(2)`; only 404 means "missing"),
m16 (missing audio file skips to next candidate; newest by
pubDate; live-enclosure episodes not re-published), m17
(warning logs for all silent skips), m18 (mkstemp payloads,
removed in finally), m20 (dedup on episode id OR (normalised
title, parsed pub_date)). Team 3 SB: identical on all four
paths.

**(b) 289 state repair — DONE:** backup
`team3/state_unsubscribe_pre289repair_2026-11-20.json` (md5
`f442e1e7…`, verified identical to pre-repair live state).
Restored the 11 published blocks verbatim, `ads_marked` 13 →
11. Publish fields kept exactly (enclosure_bytes 249754974,
suspects_bytes 12409241). Verified against live release
445857775 (assets match exactly) and the served feed (289
enclosure 249754974, old length 0 occurrences, feed md5
`05f337a3…` unchanged). Ep 288 untouched. History still holds
the stale 10 rows for 289 by design (M30 replaces them on the
next re-process; automatic run paused, no hand-edit). No
re-processing, no re-publishing. Residual, parent-verified: the
state's `bytes` field still reads 243330993 (the confounded
run's local marked file, which no longer exists on disk) — the
feed uses `enclosure_bytes` (249754974), so listeners are
unaffected, but the field does not describe the published
asset.

Application: all patches applied in staging (`apply2/`) in
order team1 (5) → team2 (11) → team3 (5). Two cross-team
conflicts resolved by hand, preserving both fixes (Team 2's
`_sorted_words` hunk at the end of `transcribe_chunked`;
Team 3's m17 logging hunks on top of m1's survivor check).
Coordinator re-ran all team verify drivers against the
consolidated staging tree (Team 2 verify_all 33/33; Team 1
verify_download 14/14 + verify_transcribe 22 + verify_bcache
7/7 + verify_misc 12/12; Team 3 Suite A 24/24 + Suite B 20/20)
and the consolidated SB harness on the fully patched tree
(exit 0), then promoted to live after md5-verified backups
(`backup_pre_fix_round/`) and re-ran Team 2's verify driver
against the live files (33/33 pass). Parent verified: all 10
live md5s match the coordinator's claims; py_compile clean;
C1 `feed_pushed` checkpoints present in publish_github.py; C2
floor re-check present (line 2163 `ne - ns >= MIN_AD_LEN`,
line 2225 `e - s < HEATMAP_MIN_AD_LEN`); consolidated SB JSON
(`team2/results/sb_coord_consolidated.json`) matches the table
above with live md5s; 289 state 11 blocks / ads_marked 11 /
publish fields intact, verified against the live release and
served feed (feed md5 unchanged).

New live md5s: run_pipeline.py
`e3f0c558ffa43d684946be4006d79310` (5,264 lines, was 4,149);
split_rescue.py `4d1d120ffba14f8a69c1412757b917f3`;
slot_align.py `9fbf2d48c0ff380bd563429ab465961b`;
transcript_diff.py `20dbe3eb765eeb6fe2f99ccf94887c1d`;
xep_recurrence.py `0c9448e50610f92c237bd990d4a7d863`;
podcast_history.py `ee9b03cbe193acc42292435889de5b1b`;
publish_github.py `ede369e35dff4f0895c6570016efd4cf`;
publish_feed.py `a2305775a60ae6702604d63e5cb1b1be`;
tmp/republish289.py `bf235a51eaf562a266ef92a312998367`;
dynamic_slots/transcribe_287B.py
`cda1c870d07ef69786b2f191d2a50ebf`. Live verified == staging
(md5s match on all 10 files). Known limitations recorded in
the team NOTEs: M1 still trusts a truncated-but-ffprobe-
parseable legacy file once; m4 accepts pre-patch chunks with no
recorded revision; cross-process load-modify-save
last-writer-wins across whole runs remains. The re-audit ("run
the audit again") is a separate step, not yet run.

---

## 55. Re-audit after the fix round — 2026-11-19/20

Owner: "write and apply fixes for all 61 findings, then run the
audit again." Three fresh teams re-audited the FIXED pipeline,
read-only, with the same subsystem split. Record:
`sb_eval/ab/audit_2026-11-20/FINDINGS.md` (736 lines), per-team
files under team1/, team2/, team3/.

Verification of the original 61: **54 FIXED, 7 PARTIALLY
FIXED, 0 NOT FIXED.** The 7 partials, with the residual each:
C1 (publish_github.py has the feed_pushed checkpoints, but
tmp/republish289.py still saves state before the feed push and
sets no flag); C2 (the 6.5s shard is gone, but the re-check
enforces only the 30s floor, not the 45s single-method floor —
a 40s single-method block is kept; documented design choice);
M2 (probe-0 and empty-transcript guards work, but 5 words / 5s
still passes — the rate floor only applies at duration ≥ 60s);
M23 (snap direction corrected at all three named sites, but a
raw edge falling strictly inside a silence is never snapped,
and find_opening / _first_opening_cue_after still return the
silence start); M34 (flag and attempts persisted immediately
with a crash cap of 3, but the no-slots streak / flag-clear is
still in-memory-only); m13 (word ordering fixed and confirmed,
but the detection horizon is still the last transcribed word,
not the audio); m19 (corrupt chunk/transcript handling works,
but IndexError is in neither corrupt-cache except tuple).

**M19 INVALID verdict: CONFIRMED CORRECT.** Team 2 re-ran the
measurement driver against live slot_align.py: all 18 frozen
gated slots reproduce exactly; false slot 0.176 vs lowest true
0.677 (gap 0.501, floor 0.5 sits in it). Options A/B collapse
or invert the gap. No behaviour change is the right outcome.

A transcript cache fix: FIXED in code, NOT YET REMEDIATED on
disk. The a_identity.json sidecar works (mismatch and
missing-identity caches are dropped and re-transcribed), but
289's A dir is still in its pre-fix state: 35 chunk files /
19,074 words vs transcript.json 18,830 words / 6068.98s, stale
chunk_034.json (244 words) still present, no a_identity.json,
no download sidecar. No run has occurred since the fix
(nightly paused). A sweep of all 21 on-disk episode dirs found
289 as the only chunk/transcript mismatch; the stale chunk is
currently harmless (all live readers filter by duration) and
will sit there until 289 is re-processed.

289 state repair: VERIFIED COMPLETE. Live state lists exactly
the 11 published blocks, matching snapshot_pre.json exactly
(edges, sponsors, sources); ads_marked 11;
ads_marked_seconds 600.6337190000007; enclosure_bytes 249754974
and suspects_bytes 12409241 intact; local feed.xml carries
249754974 once, old 249754901 zero times; 288 untouched (12
blocks, no enclosure).

Remaining findings — **1 CRITICAL, 7 MAJOR, 22 MINOR (30
total)** (31 raw team findings minus 1 duplicate merged).
**CRITICAL: T2-N3 — Generic label parts collide as sponsor
identity, deleting a whole ad.** `_merge_slots` and the
heat-map gap-merge compare label parts; every weak-stripped
block has part `ad` and every voice/reverb-only region has
part `dry-island`. Repro: two `voice/reverb:ad` blocks + one
slot → one block, second ad deleted; two voice+reverb regions
(Acme 100–160s, Beta 175–235s) gap-merge into one block (100,
236, 'reverb/voice:ad'). Parent confirmed both comparison
sites in the live code (`_merge_slots`: the C3 collapse test
`len({_xep_part(...)}) != 1`; gap-merge:
`merged_blocks[-1][2].split(':')[-1] == l.split(':')[-1]`).
Fix direction: never collapse/merge on a generic/artifact part
alone. **MAJOR (7):** T1-N7 (a truncated-but-parseable legacy
file is blessed permanently, not "once" — measured: an mp3
truncated to 2% of its bytes still probes at full header
duration (120.0s), so ffprobe cannot detect truncation; no
live instance found, but every live file is currently
sidecar-less); T2-N1 (pause-snapping misses a raw edge inside
a silence); T2-N2 (find_opening / _first_opening_cue_after
return the silence START); T2-N4 (split-rescue can emit two
overlapping blocks for the same sponsor from one region);
T2-N5 (a slot over an ad block AND a following plug block
overwrites the plug); T3-N1 (publish_feed.py sets
feed_pushed=True after a local-only feed regeneration; the
served feed is never pushed — fix-introduced, the C1 shape
reintroduced by the fix's own flag assignment); T3-N2
(publish_github.py reuses a release asset by name + size, so an
edge-only re-mark silently reuses the stale asset — reproduced:
two chapter sets, same sponsors/titles, different edges →
identical size (1,924,259 bytes), different SHA-256; the reuse
branch selects the stale asset and verification passes
trivially — fix-introduced). **MINOR (22):** T1-N1…N6, N8–N12
(cache-drop failures swallowed; rejected pin silently reused;
plausibility guard has no absolute floor; IndexError still
missed; slots_only_mode reads the chunk cache with no
guard/identity check; pre-patch chunks accepted with no
recorded revision — known limitation (b), confirmed real;
unverified pinned B accepted on the duration guard alone; XEP
cache key doesn't bind span parameters; _retry_dynamic_only
trusts stored transcript + on-disk A with no identity check;
fetch_transcript small-body error bypasses retry/curl fallback;
dynamic_slots/align.py still has its own unfiltered
load_words), T2-N6…N8 (weak-stripped blocks reported as missed
sponsors; detection horizon still the last transcribed word;
multi-token matching asymmetric on singular/plural), T3-N3…
N9, N11 (republish289.py M37 branch clears suspects fields
while the asset is still live; save_state backup stamp is
1-second resolution — 8 rapid saves produce 1 .bak, not 5;
save_state archives a corrupt current state.json as a backup
without validating it; M30 replace drops verified=true flags —
23 live verified rows affected; republish_needed is
edges-only, so a sponsor rename never flags a re-publish;
no-slots streak still in-memory-only; cross-process
load-modify-save last-writer-wins remains — fix team's
recorded limitation (a), confirmed by repro;
tmp/retry289_final.py is a stale driver that would clobber the
repaired 289 state if re-run). Fix-introduced findings: T3-N1,
T3-N2, T3-N3, T3-N6, plus new-code T3-N4, T3-N5, T3-N7 (7).

SB re-measurement: Team 2's independent run of the existing
harness on the live tree, fresh variant dirs, frozen data only
— **exact match, zero diff** (detector 0.757 (81/107)/FP
37/median 4.8s/within-1s 25.9%; detect-only 0.738 (79/107)/FP
31; slot-flow 0.738 (79/107)/FP 38; full 0.757 (81/107)/FP
44; headlines, aggregates, and all 25 per-episode scores
identical to sb_coord_consolidated.json; only difference
anywhere: a run-time timestamp string in one episode's slot
log). Coverage: all three teams fully read their subsystems
end to end; not finished: the detection core (~lines 666–3070
except cited spots) for Team 3, and cross-team internals
outside each team's subsystem. Checked-and-benign, not filed:
detect_narrative_breaks is dead code; find_pause_before/after
docstrings inverted vs correct code; B-only retry's rescue
no-op; _save_state_quietly swallowing saves by design;
publish_feed's unvalidated json.loads (pre-existing); M35's
missing item-count check. Noted, not filed: stray inert file
pipeline/run_pipeline.py.orig (178 KB, Nov 18) sits in the live
pipeline dir. Uncertain items (stated as such, not findings):
T3-N8 rests on a static trace (no repro run); T1-N7's "no live
instance" is bounded by the 21 retained transcripts' word
rates. Live tree unchanged by the re-audit (run_pipeline.py
md5 still e3f0c558…), confirming read-only. Parent verified:
FINDINGS.md 736 lines; live md5s unchanged; SB re-measurement
JSON matches the fix round exactly; both T2-N3 comparison
sites read in the live code.

---

## 56. Fix round for the re-audit's 1 critical + 7 major — APPLIED 2026-11-20

Owner (2026-11-20): "fix the critical and major findings
first" — the 22 minor findings stay deferred. Three fix teams
(same subsystem split), test-first, applied only if clean.
Result: **8 FIXED, 0 held, 0 invalid.** No live episode was
processed, nothing was re-published. Records:
`sb_eval/ab/fix_round_2026-11-20/` (team NOTEs with frozen
designs, test results, patch lists, SB before/after; patches
in team1/patches/ (1), team2/patches/ (5), team3/patches/
(2); consolidated SB results in
`coord/results/coord_final_results.json`).

**T2-N3 (CRITICAL) — FIXED.** Generic label parts no longer
serve as ad identity: new `_is_generic_part()` (`ad`,
`dry-island`, `voice-anomaly`, `history-prior`,
`duplicate-N`); the `_merge_slots` C3 collapse now requires
one common part that is *not* generic, and the heat-map
gap-merge refuses to merge on a generic part alone. Repro:
two `voice/reverb:ad` blocks + one slot now stay two blocks
(was one, second ad deleted); two voice+reverb regions stay
separate; same-sponsor collapse and different-sponsor keep
controls still pass. SB after N3: identical to baseline, zero
block diffs — no instance in the frozen set.

**T1-N7 — FIXED** (held by the team, applied by the
coordinator). The team's patch was held only because Team 2
was still landing fixes on the same live file; once Team 2
finished the coordinator applied it to the final live file —
dry-run clean, backup md5-verified, py_compile clean. A
parseable legacy file with no sidecar is now moved aside to
`.legacy-keep` and re-downloaded fresh once (the fresh
download writes its own sidecar); the kept copy is removed on
success or restored byte-identical on failure. Repro against
live: 18 checks, 0 holes, 0 failures. Behaviour change: the
first run after application re-downloads each sidecar-less A/B
once at processing time (all live files are currently
sidecar-less). If an enclosure URL is dead, the episode fails
that run — but the legacy file is restored intact.

**T2-N1 — FIXED.** All three snap loops now snap a raw edge
strictly inside a pause to the speech-side boundary. Repro
passes; SB headlines identical (only suspects and one
sharpness flag moved — the old wrong-early snap being
corrected).

**T2-N2 — FIXED.** `find_opening` and
`_first_opening_cue_after` now return `p_end` (speech side),
and `find_opening` locates the cue by exact phrase match.
Repro passes; SB recall/FP/median unchanged, **within-1s
improved 25.9→26.5% and within-2s 37.0→38.3%**.

**T2-N4 — FIXED.** `split_rescue.rescue_fused_blocks` now
iterates `new_blocks` (originals + rescues so far) in the
already-has-block test and both clip loops. Repro: exactly
one Y rescue, no overlaps. SB identical, rescue logs
identical.

**T2-N5 — FIXED.** New `_clip_against_plugs()` in
`_merge_slots`, applied in the single-overlap, C3-collapse,
and partial-merge paths; a slot-derived span is clipped
against partially-overlapped plug blocks, and a plug entirely
inside the span (or a <5s remainder) drops the slot. Repro:
ad (120,200), plug intact (200,280). SB identical, zero block
diffs.

**T3-N1 — FIXED.** `publish_feed.py` now sets
`feed_pushed=False` (checkpoint-1 marker) instead of `True`
after the local-only regen, so `publish_github` re-selects the
episode, pushes the served feed, verifies, and only then sets
`True`. Repro (real `main()`, mocked upload): 7/7 pass. Also
verified no live episode was ever stranded.

**T3-N2 — FIXED.** Asset reuse now requires name + size +
content identity: the local file's SHA-256 must equal the
episode's stored `enclosure_sha256`, recorded in state at
upload time on every upload (self-healing); no stored hash or
a mismatch → never reuse. A same-name asset that isn't reused
is deleted first via a new `_delete_release_asset` helper
(otherwise GitHub 422s on the duplicate name). Repro (audit's
two-chapter-sets case, both 1,924,259 bytes, different
SHA-256): 4/4 pass. Mock limitation: the driver's mocked
`gh("DELETE")` returns `{}`; a real successful delete returns
204, which `gh.py` surfaces as `GhError(status=None)` — the
helper treats that as success. If a delete silently failed,
the following UPLOAD fails loudly with 422, so no false
"published" claim can result. Not verified against live
GitHub (no real GitHub calls allowed).

Consolidated SB (coordinator's own run on the final live
tree, fresh variant dirs): detector **0.757 (81/107) / FP 37 /
median 4.8s / within-1s 26.5% / within-2s 38.3%** (baseline
25.9% / 37.0%); detect-only **0.738 (79/107) / FP 31**
(within-1s 27.2% / within-2s 39.2%); slot-flow **0.738
(79/107) / FP 38**; full **0.757 (81/107) / FP 44**
(within-1s 26.5% / within-2s 38.3%). Recall, FP, median,
mean, and max are unchanged everywhere; the only headline
movement is the T2-N2 edge-accuracy improvement, explained
per-episode (SN-1099 start errors moved toward truth).
Headlines, aggregates, and per-episode scores are all exactly
equal to Team 2's `sb_after_n4.json` — zero diffs, so T1-N7
is SB-neutral as expected.

Coordinator verification: all four repro drivers re-run
against the live files (T1-N7 18 checks, T2-N1..N5 17 checks,
T3-N1 7/7, T3-N2 4/4 — all pass); changed code read and
confirmed against the frozen designs at each site;
consolidated SB harness run on the final live tree (live md5s
asserted at import); 289's state still lists the 11 published
blocks with publish fields intact and 288 untouched (12
blocks, `b_failed`, no enclosure). Parent verified: all live
md5s match the coordinator's claims; py_compile clean; T2-N3
sites present in the live code (`_is_generic_part` defined
line 1608, used at the gap-merge line 2002 and the C3
collapse line 4405); T3-N1 `feed_pushed = False` at
publish_feed.py:154; T3-N2 `enclosure_sha256` /
`_delete_release_asset` present in publish_github.py;
consolidated SB JSON matches the table above; 289 state 11
blocks / publish fields intact; 288 (guid
551638f2-d62c-11f1-8c05-87721ef2056a) 12 blocks, `b_failed`,
no enclosure.

New live md5s: run_pipeline.py
`75887aab434e024ea0a6578d0d36beb4` (5,432 lines, was 5,264);
split_rescue.py `6799d9d4ab682122590f36466eefc643`;
publish_github.py `96137e70cffc5bd211a2a9afe8817166`;
publish_feed.py `67fa630f0fa9c92f2e57995a3d6643fa`. All other
pipeline files unchanged from the pre-round values. Out of
scope, noted only: the suspects-reel upload always re-uploads
with no reuse check or delete, so it carries the same latent
422 exposure on republishes. Left as-is. The 22 minor
findings remain deferred per the owner's direction.

---

## 57. Fix round for the re-audit's 22 minor — APPLIED 2026-11-20

Owner (2026-11-20): "fix the 22 minor findings." Three fix
teams (same subsystem split), test-first, applied only if
clean. Result: **21 FIXED, 1 HELD (T2-N7), 0 invalid.** No
live episode was processed, nothing was re-published. Records:
`sb_eval/ab/fix_round_2026-11-20-minor/` (team NOTEs with
frozen designs, test results, patch lists, SB before/after;
patches in team1/patches/ (4), team2/patches/ (3, incl. the
HELD t2-n7.patch), team3/patches/ (5); consolidated SB
results in `coord/results/sb_coord_consolidated.json`).

Team 1 (download/caching/transcription) — all 11 FIXED:
T1-N1 (cache-drop verified by re-glob, RuntimeError on
survivors, no identity rewrite); T1-N2 (failed pin delete
degrades to detector-only, no download call); T1-N3 (absolute
floors: duration < 60s or < 20 words); T1-N4 (IndexError added
to both transcribe_chunked except tuples, AttributeError in
the fast path); T1-N5 (slots_only_mode loads via
transcribe_chunked plus a plausibility gate); T1-N6 (accepted
as designed plus one warning per call for un-stamped chunks —
no forced re-transcription); T1-N8 (b_pin.json records
SHA/bytes/duration/verified/uses; unverified pins expire after
5 reuses, verified never expire); T1-N9 (_cache_key binds the
live span parameters); T1/T3-N10 (_retry_dynamic_only verifies
a_identity.json — SHA/size/duration ±1s — plus plausibility;
malformed words report unavailable instead of crashing);
T1-N11 (small-body error raises IOError, retryable, then curl
fallback); T1-N12 (dynamic_slots/align.py fixed in place as a
standalone CLI — filtered load_words, corrupt entries skipped
with warnings).

Team 2 (detection) — 2 FIXED, 1 HELD: T2-N6
(check_missed_sponsors time-coverage now also accepts a
generic label part via _is_generic_part — a marked-but-unnamed
block covers mentions inside it); T2-N8 (new _stem_plural
helper, strip trailing s/es only, plus stem-equality in the
multi-token branch of find_mentions; M16 "minty mobile" still
fails). **T2-N7 HELD — patch ready, do not apply as-is.** The
audio-duration horizon is correct in design (repro passes
fully) but the SB run makes numbers worse with no correctness
reason: FP +1 on every path (detector 37→38, detect-only
31→32, slot-flow 38→39, full 44→45), recall/median/within-1s/2s
unchanged. The sole change in all 25 episodes is SN-1096
gaining block (10210, 10241, `url/voice:twitter`) — Leo's
sign-off cross-promo for sister show MacBreak Weekly, a house
plug, not a paid ad; SponsorBlock correctly does not label
it. Mechanism verified against the frozen cache: voice
10210.0–10240.0 + url 10193.8–10242.1 overlap for 31s of 2
votes under the audio horizon vs 27s under the word horizon,
crossing the 30s floor. Patch preserved at
`team2/patches/t2-n7.patch` (5,064 bytes). Unblock options:
apply N7 together with a guard keeping sister-show/own-network
cross-promos out of ad marking (a new design decision), or
accept 1 FP as the price of a correct horizon.

Team 3 (state/history/publishing) — all 8 FIXED: T3-N3
(republish289.py M37 branch now consults the post-delete
remaining set — fields kept when the suspects asset is still
live, popped only when actually gone); T3-N4 (backup stamp
carries microseconds; 8 rapid saves → 5 backups); T3-N5
(save_state parses the current state before archiving; a
corrupt current state is never archived); T3-N6
(append_episode carries over verified flags from matched old
rows — data check first: live history/unsubscribe.csv holds
exactly 23 verified rows, unsub-286: 9, unsub-287: 8,
slug-266: 6, intact, no data repair needed or done); T3-N7
(_build_state_entry compares (start, end, sponsor, source));
T3-N8 (_save_state_quietly added at the end of the streak
block in _run_dynamic_for_episode); T3-N9, both halves (state
half: load_state records a snapshot + JSON-normalised
baseline, save_state merges the caller's diff onto the freshly
read disk state, per-key/per-episode/per-subkey, with in-place
refresh and fresh-entry rebuild; feed half: publish_github
re-loads state before write_feed); T3-N11
(tmp/retry289_final.py DELETED — git-format delete patch,
verified with git apply; no .py file imports or invokes it).

Consolidated SB (coordinator's own run on the fully patched
tree, fresh variant dirs): **identical to baseline on all four
paths** — detector 0.757 (81/107) / FP 37 / median 4.8s /
within-1s 26.5% / within-2s 38.3%; detect-only 0.738 (79/107)
/ FP 31; slot-flow 0.738 (79/107) / FP 38; full 0.757
(81/107) / FP 44. Headlines, aggregates, transcript_diff
and xep sections all match; the only per-episode difference
anywhere is a run-time timestamp string in one episode's slot
log.

Application: all patches applied in staging
(`coord/apply2/`) in order team1 run_pipeline → team2 t2-n8
→ team2 t2-n6 → team3 run_pipeline → single-file patches.
All dry-runs clean, no hunk failed. Single-team files verified
byte-identical to team staging (xep_recurrence,
transcript_diff, align.py, podcast_history, publish_github,
republish289). **Cross-team conflict found by the
coordinator's verification (integration fix, applied by hand
to staging only):** Team 1's N10 driver failed 1 check on the
consolidated tree — N10-A raised `TypeError: STATE_PATH is
None`. Root cause: Team 3's N9 rebuild blocks put `with
state_lock():` OUTSIDE the try that only guards
`_load_state_raw()`, and `state_lock()` itself raises
TypeError when STATE_PATH is None. The N10 fix itself works
(the B flow was called); the failure is downstream. In
production this cannot fire (main() always calls
init_podcast() first), but the N9 design's stated intent is
"fall back to the in-memory entry when the disk state cannot
be read". The coordinator restructured BOTH rebuild blocks
(_retry_dynamic_only and process_episode) so the whole locked
block is guarded by an outer try/except TypeError falling
back to the pre-N9 build-from-memory + save_state. After the
restructure: N10 driver FAILURES: none on the consolidated
tree; Team 3's N9 state + merge repros still PASS; py_compile
clean. Coordinator re-ran all team verify drivers against the
consolidated staging tree (Team 1 all PASS; Team 2 19 PASS /
5 FAIL — all 5 are the HELD N7 checks, as expected; Team 3
N4/N5, N6, N7, N8, N9, N9-merge, N3 all PASS), ran the
consolidated SB harness on the fully patched tree (live md5s
asserted at import), promoted to live after md5-verified
backups (`coord/backups/`), and re-ran Team 2's repro and
Team 1's N10 driver against the LIVE files (N6/N8/N10 pass;
N7 checks fail as expected — held) plus Team 3's N9-feed repro
against live (PASS: pushed feed has the new X enclosure,
concurrent Y enclosure kept, no stale Y enclosure).

Parent verified: all live md5s match the coordinator's
claims; py_compile clean; tmp/retry289_final.py deleted
(confirmed absent); T2-N7 patch exists (5,064 bytes); the 23
verified history rows confirmed (unsub-286: 9, unsub-287: 8,
slug-266: 6); consolidated SB JSON matches the table above;
289 state 11 blocks / ads_marked 11 / publish fields intact;
288 (guid 551638f2-d62c-11f1-8c05-87721ef2056a) 12 blocks,
`b_failed`, no enclosure. One counting note: the coordinator
reported run_pipeline.py as 5,846 lines; the live file with
the exact claimed md5 measures 5,875 lines by both `wc -l`
and Python readlines — the md5 is the identity, and the
difference is the hand-applied integration restructure landing
after the count.

New live md5s: run_pipeline.py
`ff445b8129571ebc01c78b5bf6ebfbac` (5,875 lines, was 5,432);
xep_recurrence.py `ec8aa41b7daf6497d71eb4fa2fce51c9`;
transcript_diff.py `4f1d1cd297a45a21a8a102dc1b0c6a2b`;
podcast_history.py `1ef0b5e9b961d3f2463441da8748a38b`;
publish_github.py `bc8e389eb27fcda4c077cc7992525337`;
dynamic_slots/align.py `020eb20db181f266c077e5975549e378`;
tmp/republish289.py `a3028314c8e8dd70faf027a10ba26348`;
tmp/retry289_final.py DELETED. split_rescue.py,
slot_align.py, publish_feed.py unchanged. Live verified ==
staging (md5s match). Behaviour changes flagged by the
teams: N9 invalidates existing `xep_spans.json` caches once
(cheap recompute); N10 costs legacy episodes without
`a_identity.json` the B-only shortcut until fully processed
once (no live pending episode); N3 refuses any
sub-60s/sub-20-word transcript; N1 fails an episode loudly on
a failed cache drop; N2 degrades to detector-only on a failed
pin delete; N8 bounds unverified-pin exposure to 5 reuses.
The suspects-reel upload still re-uploads with no reuse check
or delete (same latent 422 exposure noted in the previous
round) — out of scope, left as-is. **T2-N7 is the only open
item from this round** (held, patch preserved, unblock options
above).

---

## 58. T2-N7 applied with the cross-promo guard — APPLIED 2026-11-20

Owner (2026-11-20): "do them both" — apply T2-N7 with the
cross-promo guard, then re-process 289. This entry covers the
T2-N7 half; the 289 re-process is a separate step. Single
agent (not a team round). Result: **FIXED, live.** Record:
`sb_eval/ab/fix_round_2026-11-20-n7/` (NOTE.md with the
frozen design, test results, merge record; patches
`t2-n7.patch` + `t2-n7-guard.patch`; drivers
`guard_t2n7.py` / `repro_t2n678.py` / `run_sb.py`; results
`results/sb_patched.json`; backup in `coord/backups/`).
HANDOFF.md item 35.

Guard design (generic terms): a horizon-extension content
gate at both heat-map block-emission sites in
`detect_ad_blocks` — a block whose 2+-vote run clipped to the
word horizon is below the 30-second floor is marked only if a
*content* method — CTA, brand, cluster, cluster-popular,
duplicate, publisher chapter, or brand-orphan — voted
somewhere in the block. Otherwise it is dropped and stays a
suspect, exactly as before N7. URL, voice, reverb, and
history-preroll do not count as content: a spoken URL can be
the show's own site, voice/reverb are delivery anomalies, and
history-preroll is corroboration from other episodes. The
guard is inert when no audio duration is passed (horizon
equals word horizon), is scoped to heat-map blocks only, and
references no show, network, sponsor, or house-plugs config.

Verification: N7 patch dry-ran clean against live (all 9
hunks) and applied to a staging copy; N7 repro 24/24 PASS;
the SN-1096 single-episode run showed the cross-promo block
(10210, 10241, `url/voice:twitter`) — defect reproduced on
the frozen data. Guard driver 4/4 PASS (staging and live):
the horizon-dependent {url, voice} block is dropped; the same
shape with a brand-orphan content vote is kept as (90, 151);
a non-horizon-dependent {url, voice} block is kept (edges
identical on N7-only staging, proving the guard does not
touch it); the fallback without audio_duration is unchanged.
N7 repro 24/24 PASS on the guarded staging and on live (P6f
still passes — its {url, brand-orphan} block carries a
content vote). SN-1096 with the guard: the (10210, 10241)
block is absent; 7 detector blocks, all identical to the
N7-only run; FP 1 (baseline; N7-only was 2). Fallback check:
detector blocks on SN-1096, unsub-287, and lex-fridman
without audio_duration are identical to the frozen baseline
on all three. Full SB set (N7+guard), all four paths
identical to baseline: detector 0.757 (81/107) / FP 37 /
median 4.8s / within-1s 26.5% / within-2s 38.3%; detect-only
0.738 (79/107) / FP 31; slot-flow 0.738 (79/107) / FP 38;
full 0.757 (81/107) / FP 44. Per-episode scores identical on
all 25 episodes; the sole difference anywhere is a run-time
timestamp string in one episode's slot log.

Application: backup md5-verified, both patches applied to
live (dry-run clean, all hunks), py_compile clean. New live
md5: `6175c75fec20536d719d4ab6f3aa5b9b` (5,917 lines), diff vs
the tested staging file empty. All other pipeline files
unchanged. 289 state still 11 blocks with publish fields
intact; 288 untouched (12 blocks, `b_failed`, no enclosure).
No episode was processed and nothing was re-published. T2-N7
is now closed — all 61 original findings and all 30 re-audit
findings are resolved. Parent verified: live md5 and line
count match the agent's claims; py_compile clean; the guard
sites present in the live code (audio-duration horizon lines
2072–2080, content-vote gate line 2256, _method_chapter
audio_duration lines 3524/3540, call sites 5246/5589); the SB
results file (`results/sb_patched.json`, live md5 asserted
inside) matches the table above on all four paths; 289 state
11 blocks / publish fields intact; 288 12 blocks, `b_failed`,
no enclosure.

---

## 59. Unsubscribe 289 re-process with the fixed pipeline — PUBLISH GATE FAILED, NOT re-published 2026-11-20/21

Owner (2026-11-20): "do them both" — the second half, after
T2-N7 (entry 58). Single agent, driver
`tmp/retry289_n7.py` (new; retry289_final.py was deleted in
the minor round and was NOT recreated or re-run), log
`tmp/retry289_n7_2026-11-20.log`, gate record
`tmp/retry289_n7_gate.json`, state snapshot
`tmp/state_unsubscribe_pre_n7_2026-11-20.json` (md5
`ca70dd9707143993015eca3be36bac75`), gate diagnostic
`tmp/gate_diag289.py`. HANDOFF.md item 36. Result: the new
run produced 11 blocks, but only 1 of the 11 matches the
published 11 within 2 seconds. The published file stands
unchanged, and the state was restored to the pre-run snapshot
so it stays consistent with the served feed.

The run: pipeline verified before starting
(run_pipeline.py md5 `6175c75fec20536d719d4ab6f3aa5b9b`,
5,917 lines). **First run killed by SIGTERM at ~59 minutes**
(the runtime's ~60-minute background limit) during
`_write_marked_file`, after the A download + full A
re-transcription + detection + B re-transcription + slot flow
had all completed. The feed-site marked file was left partial
(75,716,151 bytes). The state was unharmed (the 289 entry
was byte-identical to the snapshot; only `dynamic_ads
consecutive_no_slots` moved 0 → 1). **Resume run completed
in ~7.6 minutes, exit 0**, with warm caches.

Fresh A (Megaphone rotation) and pinned B: fresh A
250,312,988 bytes, sha256 `a24107c7…`, duration 6,249.613s —
the **full cut**, not the short ~6,069s cut, so the 102:41
tail block was in range (the expected caveat about a missing
tail block did not apply). B stayed pinned throughout: sha
`c48dc606`, 245,506,466 bytes, 6,129.5s, marker matches
(verified before and after). Its transcript chunks were
re-transcribed once (35 chunks, 18,780 words) because the
A-identity check found no `a_identity.json` in the B dir on
the first run; the B dir now carries one, plus `b_pin.json`
(uses: 1, unverified, expires after 5) and a sidecar, so
future runs reuse the B cache.

What the run proved about the fixes: **T1-N7 engaged.** The
sidecar-less A was moved aside to `.legacy-keep` and
re-downloaded fresh once; the sidecar was written with
`content_length_verified: True`; `.legacy-keep` was removed
on success. **A-cache fix engaged and the A dir is now
remediated.** Stale chunks were dropped and the A was fully
re-transcribed (35 chunks, 19,172 words). `a_identity.json`
is present; the stale `chunk_034` is gone (fresh `chunk_034`:
256 words, 6120.0–6248.44s, ending "Experian!"). Chunk word
sum (19,172) equals `transcript.json` (19,172). Max word end
6,248.4s. **T2-N7 guard did NOT fire — correctly.** No
"dropped: horizon-dependent" line in either run's log. The
tail block 6214–6246 sits fully inside the word horizon
(last word 6,248.44s), so no horizon-dependent block arose,
and no cross-promo false block appeared. The guard is present
and not misfiring, but it was not exercised by a true
cross-promo case on this episode. **Slot flow: zero gated
slots.** The aligner found 9 slots (differing-token rate
0.1263, 12,386 anchors, a_duration 6,248.4, b_duration
6,126.6); the gate rejected all 9 for `no_ad_evidence`.
`dynamic_status=no_slots`; `dynamic_ads
consecutive_no_slots` is now 2.

Gate diagnosis — the key finding (why all 9 slots failed):
`tmp/gate_diag289.py` (exact `_slot_gate_b_side` calls + a
vote-based A-side detector check against the cached
transcripts) shows **all 9 slots fail all three sub-gates**:
A-side mention: none — no description-sponsor or house-plug
mention falls inside any slot. A-side detector: none — no
cta/cluster/brand/chapter candidate overlaps any slot by ≥5s
and ≥10%. The detector blocks in those regions were built
from duplicate/reverb/voice/url/brand-orphan votes only.
B-side: FAIL — no brand mention in any mapped B span; only
one slot has any URL fragments (1417 slot: `.com.` at
1446.72, `.org` at 1410.4), with no brand to pair. **Root
cause, confirmed against the 2026-11-19 second re-run log:
the M17 gate tightening from the 61-fix round.** On the old
pipeline the same Vsafe slot gated with `evidence=detector`
(old gate: any non-reverb overlap > 0; the Vsafe candidates
were duplicate/reverb/url). Under M17 those methods no longer
count, so dynamic ads for non-description brands (Carvana,
Vsafe, Experian, the three unnamed ads) cannot gate — and the
B-side cannot save them because the B's replacement ads are
for brands outside the description sponsors. The SB
validation set never caught this: SponsorBlock labels are
description sponsors only. This is a **design consequence of
M17, not a code bug** — a scoped refinement (e.g. accepting
"slot overlaps a 2+-vote detector block" as evidence) needs
the owner's direction. It was not fixed unilaterally.

New blocks vs published 11 (per-block diff, start, end):
published 11 (state, verified unchanged): Carvana 0.0–30.0
merged; Poncho 920.16–965.3 detector; Vsafe 1398.44–1443.3
merged; Stop Box 1900.0–1935.58 detector; Ad 2298.8–2327.1
merged; Take Ultra 2847.8–2971.78 detector; Ad 3329.9–3368.0
merged; FUM 3660.0–3745.0 detector; Ad 4224.4–4253.3 merged;
Perfect Jean 4483.006281–4600.38 detector; Ad 6205.2–6228.6
slot. New 11 (all detector, `dynamic_status=no_slots`):
0.00–41.00 Ad; 920.16–965.30 Ponchooutdoors; 1420.00–1465.00
Ad; 1905.00–1940.72 Stopboxusa; 2290.00–2350.00 Ad;
2855.90–2953.52 Takeultra; 3335.00–3376.00 Ad;
3715.00–3760.00 Fum; 4235.00–4276.00 Ad; 4499.20–4605.00 The
Perfect Jean; 6214.00–6246.00 Experian. Diffs: #0 (0.0,
+11.0), unnamed; #1 (0.0, 0.0) **PASS** (Poncho, the only
match); #2 (+21.56, +21.7), unnamed; #3 (+5.0, +5.14); #4
(−8.8, +22.9); #5 (+8.1, −18.26); #6 (+5.1, +8.0); #7 (+55.0,
+15.0) — FUM's start moved 55s; #8 (+10.6, +22.7); #9 (+16.2,
+4.62); #10 (+8.8, +17.4), renamed — the tail is now labeled
**Experian** (the fresh A cut carries an Experian ad at the
tail instead of the Fockers copy). Gate: 1 of 11 match →
**FAIL**.

Live state after the run (verified): 289 state entry
identical to the pre-run snapshot (restored): exactly the
published 11 blocks, `ads_marked` 11, `enclosure_bytes`
249754974, `suspects_bytes` 12409241, `enclosure_url`
matches, `republish_needed` cleared. The residual `bytes`
field (243330993, the confounded run's local file) is
unchanged and harmless. Release 445857775 unchanged: marked
249,754,974 bytes, suspects 12,409,241 bytes. Served feed
unchanged: md5 `05f337a3…`; 289 enclosure length 249754974 (1
occurrence), old length 249754901 (0 occurrences). 288
untouched: 12 blocks, `b_failed`, no enclosure. History:
M30 replaced 289's rows — 11 new rows (episode_id = guid
`55863d46-d62c-11f1-8c05-43d5e288331b`, `pipeline_version`
6175c75fec20, `processed_at` 2026-11-21, duration 6249.6).
Local feed-site files diverged from the published assets
(this run's files, not published): marked 250,563,359 bytes,
md5 `b921933f3b7c4ab8da228df21044b72f` (published 249,754,974
/ `405ff645…`); suspects 10,409,523 bytes (published
12,409,241). The marked file is complete (duration
6,249.613s, 11 ad chapters). Also noted: `missed_sponsors`
lists `stopboxusa` with **0 transcript mentions** on the
fresh A — the Stop Box block (1905–1940.72) was named from
the URL stem `boxusa` alone. The run log warned "sponsor
'stopboxusa' not in audio (0 transcript mentions)" in both
runs.

Parent verified: state entry identical to the pre-run
snapshot (exact equality); gate record `passed: false` with
the per-block diffs above (Poncho the only match, edges
0.0/0.0); release assets unchanged; served feed md5
unchanged with the correct enclosure length; A dir
remediated (a_identity.json present with the fresh A's SHA
`a24107c7…`, size 250312988, duration 6249.612878; chunk word
sum 19,172 equals transcript.json 19,172; fresh chunk_034
256 words, 6120.0–6248.44s, ending "Experian!"); 288 12
blocks, `b_failed`, no enclosure; history 11 new 289 rows
(pipeline_version 6175c75fec20, processed_at 2026-11-21).
Open: the M17 slot-gate consequence (zero gated slots for
dynamic ads of non-description brands) is the main
unresolved technical question from this run; it needs an
owner decision on a scoped gate refinement before any
re-process could pass the gate on an episode like this.

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
