# Podcast Ad-Marking Pipeline

Server-side pipeline that downloads podcast episodes, transcribes them,
detects sponsor ad reads with a multi-method heat map, and marks each ad
as a chapter titled "Muse Detected Ad For \<company or product\>" in the
full original audio — nothing is cut. House plugs (a show's own
platforms/memberships, e.g. Pepperbox, Club TWiT) are a subtype of
advertisement: detected and marked the same way, but titled
"Muse Detected House Plug For \<name\>", never as an ad. It publishes
the marked episodes to GitHub releases with a regenerated RSS feed for
Podcast Addict.

Chapters model since 2026-10-28 (per the owner): previously the pipeline
cut ads out (with a swish at every splice) and published cleaned
episodes plus a QA reel. On 2026-10-28 all previously published edited
episodes were deleted, all three feeds were reverted to the original
publisher audio, and every past episode was marked skipped — the
chapters model applies forward only.

This repo mirrors the live pipeline. A daily sync job pushes code and
config changes from the working copy. If you change the pipeline, sync
this repo in the same step (see "Keeping this repo in sync").

**Test log:** [`TESTS.md`](TESTS.md) is the canonical log of every
detection and transcription experiment tried on this pipeline — why
each was a candidate, how to reproduce it, the numbers, and the
verdict (kept / discarded / undecided). Read it before re-proposing a
mechanism.

## What it produces

- **Marked episode MP3** (full original with ad chapters embedded) as a
  GitHub release asset.
- **Suspect reel** (2026-10-22) — `<slug>-suspects.mp3`, a release asset (NOT
  a feed item): segments the pipeline was unsure about — low-confidence
  candidates (voice anomalies, cluster-popular hits, brand-orphan windows)
  that did not become marked blocks — pasted chronologically, one chapter per
  segment. Delivery (2026-10-22): the nightly worker sends the reel file to
  the podcast side chat, where it is posted as a playable audio attachment
  — never as a GitHub link in the main chat, because release links force a
  download instead of playing. Suspect timestamps are given in
  minute:seconds. Never mark from the suspect reel without the owner's approval.
- **Marked-ads summary** in the episode description (plain text, at the TOP
  below a dashes dividing line, each marked ad with its start time).
- **Regenerated RSS feed** (`feeds/<Podcast-Name>` in the Clean-Podcasts
  repo) for Podcast Addict.
- **Per-podcast history file** (2026-11-14) — after each processed
  episode, its detected ads are appended to a CSV (one row per ad:
  episode, ad index, sponsor, dynamic or host-read, start/end in
  seconds and as a percentage of playtime, duration, boundary
  sharpness, methods that voted). The file is local working data
  (not published); it records what the detector found so ad-load
  patterns per show can be learned from later. Collection only —
  no history-based voting exists yet.

## Repository layout

- `pipeline/run_pipeline.py` — the engine: fetch, transcribe, detect, mark
  ad chapters, generate feeds.
- `pipeline/podcast_history.py` — per-podcast ad history: append each
  processed episode's blocks to a CSV and compute per-slot averages
  (used for learning ad-load patterns; collection only, no voter).
- `pipeline/publish_github.py` — publish the newest marked episode:
  GitHub release + feed push to the Clean-Podcasts repo.
- `pipeline/regen_feed.py` — regenerate a feed without processing episodes.
- `pipeline/publish_feed.py` — LEGACY publisher (remote-storage upload);
  superseded by `publish_github.py`. Kept for reference.
- `pipeline/run_ep268.py` — example targeted single-episode run (by guid).
- `podcasts.yaml` — podcast config: feed URLs, GitHub feed paths, house
  plugs. Add a podcast here; no code changes needed.
- `whoosh.wav` — a custom swish recording, RETIRED 2026-10-28: it was inserted
  at every splice in the cutting era and is no longer used. The file is
  retained locally; do not substitute a synth if it ever returns.
- `requirements.txt` — Python dependencies.

## Setup

- Python 3.11+, ffmpeg on PATH.
- `pip install -r requirements.txt`. The faster-whisper `base` model
  (CPU, int8) downloads automatically on first run.
- GitHub publishing needs a personal access token. This environment uses
  a `gh.py` API wrapper (see the header of `publish_github.py`) with the
  token held in secure connector storage. **Never commit a token to this
  repo.**
- Local layout expected by the scripts (BASE_DIR in `run_pipeline.py`):
  `~/workspace/podcast-ads/` containing `whoosh.wav`, `podcasts.yaml`,
  `pipeline/`, and per-podcast `podcasts/<id>/state.json` plus
  `podcasts/<id>/episodes/<slug>/` working dirs (original MP3, chunk
  transcripts, marked files).

## Running

```bash
python3 pipeline/run_pipeline.py --podcast <id>     # every unprocessed episode, oldest first
python3 pipeline/run_pipeline.py --podcast <id> --limit 1   # newest unprocessed only
python3 pipeline/publish_github.py --podcast <id>  # publish newest marked episode
```

The nightly cron runs at 8:00pm CDT across all configured podcasts
(Security Now, Unsubscribe Podcast), then publishes. A full
episode takes roughly an hour (transcription dominates).

## Detection architecture (the heat map)

Every method runs independently on the **unedited** episode, then results
are cross-checked via a per-second vote heat map. **2+ votes =
high-confidence auto-marked.** Heat-map blocks may be as short as 30s (so
30-31s dynamic spots can be marked); single-method blocks keep a 45s floor.
Heat-map blocks separated by < 20s are merged when they carry the same ad
label. Block labels are picked, not alphabetical: a mention-anchored
sponsor label first, then a label with a spoken-URL anchor in the region,
then any non-artifact label. Sponsor-identified blocks
are capped to the sponsor's in-block mention span ±30s, and a
sponsor-labeled heat-map block with no mention of that sponsor within 10s
is dropped as padding/dryness noise. A single-method segment that
overlaps a heat-map block for the same sponsor extends the block's
start to the segment's start (2026-11-07: the segment, often the
cluster, frequently starts where the ad starts, while the heat-map
block starts only where a second vote arrives at the CTA/URL tail;
the extension stops at the ad's opening cue when one is spoken in
between, so a cluster that starts early on content mentions of a
common-word sponsor cannot over-extend). The show's own domains
(derived generically from the RSS channel link and non-sponsor
description links) are excluded from URL evidence (2026-11-07).
Finally, in middle-ground mode
(2026-10-29, replacing the same-day zero-over-mark shrink that the owner
rejected for gutting sparse-mention reads — Stop Box 51s → 13s, Club
TWiT 75s → 2s), each block edge is refined inward to the nearest
method-segment boundary lying between the block edge and the nearest
in-block brand mention; edges with no segment boundary in the interval
stay put, and the refined span always contains the mention span.
CTA/URL bounds are asymmetric for edge purposes only (voting uses the
raw segments): starts stay raw, because an opening cue precedes the
first brand word; ends are trimmed to the closing evidence sentence,
because expansions overshoot past it. Chapter boundaries land on clean
sentence breaks (pause-snapped). Ad-block bounds: 720s
maximum.

| Method | What it detects | Confidence |
|---|---|---|
| `cta` | Generic URL / promo-code CTA patterns (10s floor for short spots); toll-free phone numbers (`1-800-GOT-Junk`) are CTA anchors like URLs, with a tight-cluster expansion fallback for produced spots with no host cue | High — marks alone |
| `url` | Spoken URLs with NO CTA phrase (2026-10-28): dynamic spots often say just "at leaffilter.com slash gold" with no CTA phrase, so the CTA gate never anchors them. A spoken domain is itself strong ad evidence; the segment runs [first URL −45s, last URL +10s] (ads put the URL at the end) | Low — heat-map votes only, never marks alone |
| `brand` | Description sponsors as ground truth (fuzzy matching, incl. Whisper mistranscription variants) | High — marks alone |
| `cluster` | Dense brand mentions: 4+ in 1 minute (casual ad reads); segments bounded to the mention span ±60s, and single-method cluster blocks need a spoken URL for that sponsor nearby (a place name can fake dense mentions — 287 "rattlesnake Ridge") | High — marks alone |
| `duplicate` | Verbatim-repeated segments ≥15s (8-word shingles); inserted ads run 2+ times | Low — heat-map votes only, never marks alone (2026-11-07: duplicate-only blocks were false positives on repeated intros and re-read quotes; a duplicate segment must corroborate another method's vote) |
| `chapter` | Publisher-labeled ad chapters (`[Ad]`, `Ad:`, `Advertisement`, `Sponsor`, `Commercial`, `Ad break`) from ID3 tags and RSS `<psc:chapter>` | High — marks alone |
| `brand-orphan` | Sponsor mentions the brand method found but could not expand into a segment (no opening cue) | Low — heat-map votes only, never marks alone |
| `cluster-popular` | Dense mentions of popular sponsors NOT in the description | Low — heat-map votes only; single-vote hits go to the suspect reel, never auto-marked |
| `voice` | Host-voice anomaly (MFCC profile distance) | Review only — suspect reel, never marks alone |
| `reverb` | Room-echo dryness vote (2026-10-27): ad segments on conversational shows are recorded drier than the episode's reverberant content (envelope late-autocorrelation). Gated to reverberant episodes (median ≥ 0.25) with an adaptive threshold, so it stays silent on dry professional shows | Low — heat-map votes only, never marks alone |

Notes:

- `detect_narrative_breaks` (sentence-embedding narrative jumps) is defined
  in `run_pipeline.py` but **not wired into detection**. Tested twice and
  rejected: TF-IDF (2026-10-28) fired 141/163 segments on 286/287 (near-zero
  lexical overlap between adjacent conversational windows), and real
  all-MiniLM-L6-v2 embeddings (2026-10-28) still gave 57 segments per
  episode with 27–33 false segments covering 6,013–11,447s — conversational
  topic drift triggers "islands" everywhere, and episode-start ads are
  structurally invisible (no previous window). Do not wire it in without
  a separation result on fresh ground truth.
- **Middle-ground boundary refinement (2026-10-29, owner decision):**
  the zero-over-mark shrink from earlier the same day was rejected
  (it collapsed sparse-mention reads: SN 1101 Club TWiT 75s → 2s,
  Stop Box 51s → 13s) and replaced by edge refinement: each block
  edge moves inward to the nearest method-segment boundary between
  the block edge and the nearest in-block brand mention, or stays
  put; CTA/URL ends are trimmed to their closing evidence sentence
  for edge purposes (starts stay raw, since opening cues precede
  brand words). Validated: 286 over-mark 128s → 18s with coverage
  608/673s, 287 over-mark 176s → 11s with coverage 463/531s, zero
  false blocks on both, all 17 ads chaptered (no degenerate
  chapters — smallest are the 18s and 24s Leaf Filter spots); SN
  1101 regression: same 6 blocks, no false blocks, Club TWiT a
  54s chapter and ThreatLocker 76s (not collapsed). The old
  "caught ≥80% covered" metric reads 7/9 and 6/8 under this mode —
  the two misses per episode are coverage-short, not missing
  chapters. Also fixed the same day: `fetch_feed` retries truncated reads
  (the TWiT feed twice stalled at ~196KB via urllib while curl fetched
  all 232KB) — retries raised to 5, plus a curl fallback after
  urllib retries are exhausted (the proxy sometimes stalls urllib
  at exactly the same byte count on every attempt while curl
  fetches cleanly in the same window).
- **Unsubscribe 286 + 287 ground-truth validation (2026-10-28):** The owner
  supplied all ad timestamps for both episodes (286: 9 ads; 287: 8 ads).
  Before this round the pipeline marked 286 7/9 (Ozempic, Leaf Filter
  missed) and 287 7/8 with 2 false blocks (a "RIDGE" block on a
  rattlesnake-range discussion, a "Factor" block on gun-manufacturing
  talk). Fixes implemented and re-validated: the `url` vote; a block-label
  picker (mention-anchored sponsor label, then URL-anchored label, then
  any non-artifact label — the old alphabetical-first rule misnamed Leaf
  Filter "Indeed" and Ridge/Got Junk "dry-island"); fuzzy threshold
  0.80 → 0.85 plus a prefix-suffix guard (`factory` is not Factor,
  `progressively` is not Progressive, `need` is not Indeed) and a
  join-branch containment guard (any word before `factory` counted as
  `factor`); cluster segments bounded to mention span ±60s (an unbounded
  Poncho candidate bridged Leaf Filter #2 and Poncho into one run that
  the cap trimmed to Poncho alone); gap-merge only across equal labels;
  single-method cluster blocks require a spoken URL nearby. After the
  fixes: **286 9/9 with zero false blocks and 128s over-mark;
  287 8/8 with zero false blocks and 176s over-mark.** Security Now 1101
  regression: same 6 blocks, no false blocks, only cap/bound tightenings.
  Nothing was republished — 286/287 stay reverted/skipped; the fixes apply
  to future episodes.
- **Loudness/compression: tested 2026-10-28, REJECTED.** Produced spots
  are not louder or more compressed than content on these feeds (286 ad
  vs content RMS medians −17.4 vs −18.1 dB, crest 15.4 vs 16.3 dB;
  threshold rules peaked at 0.10 precision). On 287 the rule separated
  host-read energy from quiet debate content but voted the already-caught
  host reads and missed every dynamic spot that needed help (Progressive,
  Leaf Filter ×2 are quieter than content). Do not add a loudness vote
  without a separation result on fresh ground truth.
- **Domain-stem matching (2026-10-22):** sponsor variants include the bare
  domain stem, so dotted domains with modern TLDs (`canary.tools`) match
  the spoken brand word. SN 1101's Thinkst Canary read was missed because
  the variant set contained only `canary.tools` / `canary tools`, which
  never match transcript tokens (Whisper splits spoken URLs into separate
  words, and may mangle the TLD). The TLD-strip lists in
  `extract_sponsors` and `find_url_mentions` must stay in sync — a missing
  TLD is a silent miss.
- **Unsubscribe 286 ground-truth validation (2026-10-27):** superseded by
  the 2026-10-28 entry above — the pipeline then marked 7/9 with 169s
  over-mark; the `url` vote and the other 2026-10-28 fixes took it to 9/9.
- **Dynamic-slot integration (2026-11-11, implemented):** a second
  download of the same episode, transcribed and aligned against the
  first (5-word-shingle anchors, density filter on differing tokens),
  reveals the dynamic-ad slots. The B flow runs after A detection,
  before chapter building, on dynamic-capable feeds only (probe once
  per podcast by byte-compare, 30-day re-probe, optional
  `dynamic_ads` override in `podcasts.yaml`); copy A is always the
  published copy and chapters live on the A clock. Each slot must
  pass an evidence gate (a detector method segment overlapping it,
  a sponsor mention inside it, or a brand + URL/CTA in the B-side
  copy) and is named from A-side evidence only. Exception (2026-11-11,
  owner): a feed carrying `podcast:transcript` tags skips the second
  download entirely — the publisher transcript is already a second
  transcript to compare against; an explicit `dynamic_ads: true` in
  `podcasts.yaml` beats the exception. A gated slot
  overlapping a detector block takes the slot's edges + the
  detector's label (no refinement on slot edges); a slot with no
  detector block becomes a new chapter; slot blocks bypass the
  30s/45s floors; house-plug blocks are never overwritten. Sanity
  guards abort slots for the episode when the differing-token rate
  exceeds 25%, the durations differ by over 10%, or gated slots
  exceed 25% of the episode. Validated on the owner's 286/287
  ground truth: all 7 dynamic ads found with mean edge error 0.42s
  (13/14 edges within 1s); 286 went from 7/9 to 9/9 ads at the 80%
  coverage scorer and 287 from 6/8 to 7/8, with all host-read
  blocks unchanged. Gate gap, closed 2026-11-13 and merged
  2026-11-14: on 286 a false slot inside the real Ghostbed ad (a
  transcription-hallucination region) used to pass the gate and
  become an unnamed extra block; a slot overlapping a detector
  block by under 50% now merges into the block instead — the
  Ghostbed block becomes 899.4–1006.7 and 286 scores 9/9 at the
  80% coverage scorer with zero false blocks.
  A standalone `--slots-only <slug>` mode prints the gated slots
  for one episode as a manual-marking aid without touching
  pipeline state. Full numbers in `TESTS.md`.
- **Six-mechanism merge (2026-11-14, live):** the combined patch
  (1,870 lines) is now the pipeline — `pipeline/run_pipeline.py`
  plus three new modules (`xep_recurrence.py`,
  `transcript_diff.py`, `split_rescue.py`). (1) Substring-leak
  guard: short brand variants need a word boundary, so "aura" no
  longer matches "restaurant" and "stop" no longer matches
  "stopping" or "Christopher" (217 phantom hits removed, zero
  regressions). (2) Cross-episode recurrence, positive direction
  only: a sponsor recurring across the show's other episodes
  corroborates a block (recall 0.673 → 0.701, false blocks
  36 → 33 on the 25-episode set). (3) Publisher-transcript
  differential, corroboration only: where the publisher supplies
  a transcript, gaps in it are ad evidence, trimmed to the first
  and last ad word with a +0.50s end pad. (4) Gate tightening for
  partial-overlap slots (see above). (5) Back-to-back split
  rescue: a second sponsor's ad fused into one heat-map region is
  split out (recovers Ridge on the synthetic-voice test episode
  and Stop Box on the spliced test episode). (6) Density floor
  for standalone slot blocks: a slot is only marked when at
  least half its words differ between the two downloads —
  removes the one non-ad outro tail, keeps all 6 real dynamic
  ads. Post-merge numbers on the live pipeline: 25-episode set
  recall 0.701 (75/107), false blocks 38, median edge error 4.8s,
  within 1s 26.0%; slot-flow false blocks 44 → 43; 286/287 slot
  flow 286 9/9 at ≥80% (636.3/673s, 0 false), 287 7/8 (477.3/531s,
  1 false — Pepperbox house plug); SN 1101 still 6 blocks. Full
  numbers in `TESTS.md`.
- **Fusion-fix merge (2026-11-14, live):** two held patches from the
  pre-roll history-voter work (TESTS.md entries 40–41), merged on the
  owner's direction. (7) URL-anchored split rescue: when the
  mention-gated rescue finds no candidate, a second sponsor's ad can
  still be split out on a spoken-URL anchor — a `url` segment and a
  `cta` segment for the same stem, both overlapping the fused
  extension by ≥30s, the stem fuzzy-matching a description sponsor
  (≥0.85, so Whisper's "Rora" matches "Rorra"). Recovers a real Rorra
  read the mention gate could never see. (8) Pre-roll history voter
  with a bridging guard: a show with ≥5 episodes in its per-podcast
  history file votes on the window where its first ad usually starts
  (leave-one-out mean ± max(2σ, 3 points)); the vote is
  corroboration-only and can never name a block, and the guard
  punches a hole in the vote where it would connect two adjacent
  regions carrying different sponsor labels, so it cannot fuse two
  neighbouring ads. Post-merge numbers on the live pipeline:
  25-episode set recall 0.701 (75/107), false blocks 40, median edge
  error 4.8s, within 1s 26.0%; exactly three span changes vs the
  six-mechanism baseline (a Helix head extension, an Our Place ad
  and a Rorra ad recovered — adjudicated +2 real ads, 0 lost, 0
  false rescues; the scoreboard cannot see them because each is the
  second ad of a merged-pair SponsorBlock label). 286/287 detector
  numbers unchanged. Full numbers in `TESTS.md`.
- **Label-fix merge (2026-11-18, live):** three held patches from the
  autonomous improvement run (TESTS.md entries 42–44), merged on the
  owner's direction. (9) Weak-evidence labels: a block whose name rests
  only on weak methods (duplicate, reverb, voice, history) gets a plain
  `Muse Detected Ad` chapter with no sponsor name instead of a made-up
  one. (10) URL-stem guard: a name from the fuzzy URL-stem fallback is
  stripped when it matches the podcast's own identity (feed channel
  domain stem plus own domains); the production wiring extracts the
  host before stemming, since the stem function takes a host, not a
  URL. (11) Missed-sponsors check: a sponsor a block already names is
  no longer flagged missed when the transcript has zero mentions of
  it. Post-merge numbers on the live pipeline: 25-episode set recall
  0.701 (75/107), false blocks 40, median edge error 4.8s, within 1s
  26.0% — identical to the pre-merge baseline; exactly five label
  changes, zero span changes. 286/287 detector numbers unchanged. Full
  numbers in `TESTS.md`.
- **Full-pipeline re-measurement (2026-11-18):** the whole pipeline
  as run was scored on the 25-episode set (TESTS.md entry 45). Full
  pipeline: recall 0.701 (75/107), false blocks 47 raw, 35 after
  hand-checking the extras — the other 12 are real ads the answer key
  cannot see (inserted ads and second ads of merged pairs). The slot
  comparison adds no matched ads and no true false blocks; the rescue
  adds 2 matched ads and 5 real ones. The earlier full-pipeline
  figure of 45 was derived arithmetically, not measured; this is the
  first direct measurement. A full hand-check of all 35 (TESTS.md
  entry 46) found 32 are real paid ads, 2 are the show promoting
  itself, and exactly 1 is a genuine mistake — the true false-block
  count is 1 (4 counting self-promotion as false).
- **Adjudicated score (2026-11-18):** scored against the adjudicated
  truth set of 150 known real ads (TESTS.md entry 48), the pipeline
  finds 118 — true recall 0.787 — with 1 true false block (4 strict).
  The 150 is a lower bound and 0.787 an upper bound, since missed
  inserted ads are unknown; the false count of 1 is exact for marked
  blocks.
- **Slot-determinism fix (2026-11-19, MERGED):** pinning the
  second download and ignoring reverb-only evidence at the slot gate
  (TESTS.md entry 47) makes slot results identical across runs with SB
  numbers unchanged; post-merge verification on the frozen set
  reproduces every headline exactly (detector 0.701/40, slot-flow
  0.682/42, full 0.701/47).
- **Slot-start trim (2026-11-19, MERGED):** a slot head of
  three or more repeats of one short phrase is trimmed past the last
  full repeat (TESTS.md entry 49) — on episode 289 it moves the 102:41
  start from 6179.0 to 6204.9, 0.4s before the ad copy starts, with
  SB numbers identical everywhere else. Post-merge verification
  reproduces every headline exactly.
- **Ep 289 re-run (2026-11-19, publish HELD):** the re-run with the
  merged pipeline completed in ~33 min, but Megaphone served a
  truncated second download (5,496.8s vs 6,229.4s), so the duration
  guard rejected all slots and the re-run produced 10 detector-only
  blocks; the publish gate failed as designed and nothing was
  published. The blocker is the truncated download, not the pipeline —
  a plain re-run would reuse the pinned truncated copy.
- **Ep 289 published (2026-11-19, corrected slots):** after the pinned
  truncated file was deleted, a fresh second download (6,129.5s)
  produced 11 blocks — the 102:41 chapter starts at 6205.2, within
  0.1s of the ad copy start, and there is no chapter near 98:45
  (TESTS.md entry 49). One correction found during verification: the
  re-run reused the early part of the old truncated download's
  transcript, because deleting the marker file defeats the cache check
  — the 102:41 chapter is in the freshly transcribed tail and is
  correct, but the four earlier merged slots' edges are not yet
  verified against the fresh download. The cache-check fix (entry 51)
  is live.
- **Ep 289 clean re-run (2026-11-19, STOP, no re-publish):** with the
  cache-check fix live and the second download fully re-transcribed,
  the run was confounded — a new present-destination download check
  (entry 52) deleted the good cached episode audio because Megaphone
  rotates the episode on nearly every request, so detection ran on
  old words over new audio and produced 13 blocks, 3 of them false,
  with no 102:41 chapter. The published 11-block file stands and is
  verified unchanged. The download check was REVERTED the same day
  (live is now the cache-check fix only). Open items: the episode
  transcript cache needs the same SHA-based invalidation as the
  second-download cache, and state 289 now lists the 13 confounded
  blocks against the 11-block published file (decision pending).
- **Full pipeline audit (2026-11-19, analysis only, no fixes):** three
  teams read the pipeline end to end; **3 CRITICAL, 37 MAJOR, 21
  MINOR — 61 findings** (TESTS.md entry 53; record in
  `sb_eval/ab/audit_2026-11-19/FINDINGS.md`). Critical: publish
  saves state before the feed push, so a failed feed push leaves
  the episode marked published and never selected again; edge
  refinement can shrink a block far below the 30 s / 45 s floors
  with no re-check (67 s → 6.5 s in a mocked repro); one slot
  overlapping two detector blocks collapses both into a single
  block and the second ad disappears. The episode transcript
  cache has no identity check (confirmed open); two more caches
  share that gap.
- **Fix round for all 61 findings (2026-11-19/20, APPLIED):** 60
  FIXED, 1 INVALID as proposed (M19 — the density "mismatch" is
  the calibrated discriminator), 0 held (TESTS.md entry 54). The
  test set improved: detector recall 0.701 → **0.757** (75→81 of
  107), FP 40 → 37, edge accuracy unchanged. The episode
  transcript cache now has an identity sidecar, and 289's state
  blocks were restored to the published 11.
- **Re-audit after the fix round (2026-11-19/20, analysis only):**
  54 of the original 61 FIXED, 7 PARTIALLY FIXED, 0 NOT FIXED
  (TESTS.md entry 55; record in
  `sb_eval/ab/audit_2026-11-20/FINDINGS.md`). Remaining: **1
  CRITICAL, 7 MAJOR, 22 MINOR — 30 total.** Critical: generic
  label parts (`ad`, `dry-island`) collide as sponsor identity
  in the slot merge and the heat-map gap merge, deleting a whole
  ad. The test-set numbers were independently re-measured and
  match exactly (detector 0.757, FP 37).
- **Fix round for the re-audit's 1 critical + 7 major (2026-11-20,
  APPLIED):** 8 FIXED, 0 held (TESTS.md entry 56). Generic label
  parts no longer serve as ad identity; the feed script no longer
  claims the feed is pushed after a local-only regeneration; asset
  reuse now requires a matching SHA-256. Edge accuracy improved
  slightly (within-1s 25.9% → 26.5%). The 22 minor findings stay
  deferred.
- **Fix round for the re-audit's 22 minor (2026-11-20, APPLIED):**
  21 FIXED, 1 HELD (TESTS.md entry 57). The held one is T2-N7 —
  the audio-duration detection horizon is correct in design but
  adds one false block on the test set (Leo's sign-off cross-promo
  for a sister show), so the patch is preserved, not applied. The
  test set is otherwise identical on all four paths, and the stale
  289 driver that could have clobbered the repaired state is
  deleted.
- **T2-N7 applied with the cross-promo guard (2026-11-20,
  APPLIED):** the held horizon patch is live together with a
  generic content gate — a block that only reaches the floor
  because of the audio horizon is marked only if a content method
  (CTA, brand, cluster, duplicate, chapter, brand-orphan) voted in
  it (TESTS.md entry 58). The test set is identical on all four
  paths, and the cross-promo block is gone. All 61 original
  findings and all 30 re-audit findings are now resolved.
- **289 re-process with the fixed pipeline (2026-11-20/21):**
  publish gate FAILED, not re-published (TESTS.md entry 59). Only
  1 of 11 blocks matched the published 11 within 2 seconds. The
  fresh audio was the full cut, so the tail block was in range —
  but the slot flow gated zero slots: the M17 gate tightening
  means dynamic ads for brands outside the description sponsors
  can no longer gate, and the test set never caught it because
  SponsorBlock labels are description sponsors only. The A folder
  is now remediated (identity file present, stale chunk gone), and
  the published file and state stand unchanged.
- **Content-length check on downloads (2026-11-19, APPLIED):** the
  download step now compares bytes received against the advertised
  Content-Length and fails loudly on a short read (TESTS.md entry 50).
  Tested 12/12 on truncated downloads with zero detection impact; it
  only catches truncation when the server advertises a larger size
  than it sends, so the duration guard remains the backstop.
- **Ollama / LLM reviewer: evaluated 2026-10-14, REJECTED.** It hallucinated
  sponsor reads, missed real ads, produced garbled timestamps, and was
  too slow and memory-hungry. Do not re-add without a fresh evaluation.
- **SponsorBlock eval A/B (2026-11-07):** five improvements tested
  separately against 25 SponsorBlock-anchored episodes (107 labels,
  8 shows). Baseline: recall 0.636, median edge error 7.5s, within
  1s 19.9%, 67 false blocks. Winners, now in the code: same-label
  merge (recall 0.664, median 5.6s, within 1s 25.4%), duplicate
  corroboration (false blocks 67 → 45), own-domain exclusion
  (recall 0.654, median 7.0s). Combined: recall 0.673, median 4.9s,
  within 1s 25.7%, false blocks 36. Losers, removed: phonetic/fuzzy
  brand matching (Soundex+LCS — recall 0.579, false blocks 77;
  the ~480 collision hits swamped ~72 real manglings and moved
  block starts 200s+ late on Security Now) and head-extension from
  brand-seeded blocks (a wash — its Modern Wisdom target turned
  out to be label artifacts; the closing URL is already inside
  the block). A silence-based start fallback was also rejected:
  it would extend the Eight Sleep block 73s backward into the
  preceding Joovv read.
- **Brand-orphan merge (2026-11-09):** a brand-orphan segment (a
  sponsor mention the brand method could not turn into a block) is
  now a merge candidate like the same-label merge above: when it
  overlaps a heat-map block for the same sponsor, the block extends
  to the orphan segment's silence-snapped bounds, stopped at the
  first opening cue (start) or return-to-content cue (end), never
  past another sponsor's mention or into an adjacent block, and
  never double-extending a block the same-label merge already
  extended. Validated alone on the same 25 episodes: recall 0.673
  flat, median edge error 4.9 → 4.7s, within 1s 25.7 → 26.4%,
  false blocks 36 flat; 3 edges fixed, 0 harmed.
- **Boundary-confidence score (2026-11-10):** every detected block
  now carries a per-edge sharpness value (`start_sharpness`,
  `end_sharpness` in the report/state and in detection output):
  mean votes of the 3 words just inside the edge minus mean votes
  of the 3 words just outside, computed from per-word vote counts
  over the method segments. Flat edges (< 0.5) are likely bad —
  median error 14.3s vs 2.7s for sharper edges, and flat flags
  identify edges with error over 5s at precision 0.67 / recall
  0.58 on the 25-episode set — so the score is kept as a flag for
  review and as an input to the future boundary scorer. It never
  moves an edge: a repair rule built on it (flat edges move to the
  nearest direction-appropriate vote step forward in time) was
  tested alone and discarded — recall 0.673 → 0.701 and false
  blocks 36 → 33, but within-1s edges 38 → 35, because the vote
  steps sit 1–7s past the true end and the true edge is itself
  usually flat in the profile (median sharpness 0.00), so the
  steps do not locate the truth. Full numbers in `TESTS.md`.

## Standing rules (do not break these)

- The episode description's sponsor list is a **double-check for missed
  sponsors only** — never the primary ad source. Missed sponsors are
  reported (`missed_sponsors` in the report/state), never silently
  skipped.
- **Club TWiT plugs are marked like sponsor ads** on all podcasts
  (HOUSE_PLUGS in `run_pipeline.py`; `house_plugs` in `podcasts.yaml`).
- **Marked-ads summary:** plain text at the TOP of the episode description below
  the dashes dividing line (`———————————————————`), each marked ad with its
  start time in minute:seconds. **No HTML tags** — they break Podcast Addict and other
  apps' description display. Original HTML descriptions stay
  untouched; only the prepended summary is plain text.
- **Pinned originals (2026-10-31):** an episode can carry
  `pinned_original_url`/`pinned_original_bytes` in state; `write_feed`
  swaps only its enclosure to that fixed unedited download, with no
  description change and no chapters. This exists so a listener and
  the pipeline share one identical file for manual ad-marking when
  the publisher's download varies per request (dynamic ads). Clear
  the pinned fields when the episode is later marked.
- **Crash protocol:** fix the root cause, then re-run the affected episode
  from the top — full fresh run (re-download, full detection, full chapter
  marking), verified end to end. Never patch around a crash and resume.
- **Transcription word entries have LEADING SPACES** (`" dose"`). Always
  strip whitespace (use `norm_words()`) when searching transcripts.
- **Feed links for the owner** go as plain text in code blocks, `https://`
  prefix omitted — his Android long-press on links offers Reply/emoji
  only, no copy option. (Chat convention, not pipeline behavior.)

## What is NOT in this repo (stays local)

- Episode audio, transcripts, and `state.json` (7GB+ operational data).
  These are local bookkeeping only. Local `state.json` tracks what has
  been processed (including the skipped back catalog); the published
  feeds carry the marked episodes.
- Whisper model cache (auto-downloaded by faster-whisper).
- GitHub tokens or any credentials — never committed.

## Keeping this repo in sync

The live working copy is `~/workspace/podcast-ads/` and this repo's
source of truth for code/config is mirrored from it by
`sync_pipeline_repo.py` (kept alongside the pipeline, not in this repo),
run daily at 9:00pm CDT. It refreshes `pipeline/*`, `podcasts.yaml`, and
`whoosh.wav` from the live files, then uploads only files whose blob SHA
changed. The README, `requirements.txt`, and `.gitignore` live in the
mirror directory and are synced as-is. When you change the pipeline,
update the README in the same step and run the sync script immediately —
do not wait for the nightly job.
