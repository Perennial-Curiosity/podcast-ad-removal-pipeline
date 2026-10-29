# Podcast Ad-Marking Pipeline

Server-side pipeline that downloads podcast episodes, transcribes them,
detects sponsor ad reads with a multi-method heat map, and marks each ad
as a chapter titled "Muse Detected Ad For \<company or product\>" in the
full original audio — nothing is cut. It publishes the marked episodes
to GitHub releases with a regenerated RSS feed for Podcast Addict.

Chapters model since 2026-10-28 (per the owner): previously the pipeline
cut ads out (with a swish at every splice) and published cleaned
episodes plus a QA reel. On 2026-10-28 all previously published edited
episodes were deleted, all three feeds were reverted to the original
publisher audio, and every past episode was marked skipped — the
chapters model applies forward only.

This repo mirrors the live pipeline. A daily sync job pushes code and
config changes from the working copy. If you change the pipeline, sync
this repo in the same step (see "Keeping this repo in sync").

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

## Repository layout

- `pipeline/run_pipeline.py` — the engine: fetch, transcribe, detect, mark
  ad chapters, generate feeds.
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
(Security Now, Unsubscribe Podcast, Lore), then publishes. A full
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
is dropped as padding/dryness noise. Finally, in middle-ground mode
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
| `duplicate` | Verbatim-repeated segments ≥15s (8-word shingles); inserted ads run 2+ times | High — marks alone |
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
- **Dynamic ads:** two downloads of one episode differ; transcript
  differences over 20s reveal the ad slots, which are consistent across
  downloads. Slot positions map via offset subtraction (costs 2x
  transcription).
- **Ollama / LLM reviewer: evaluated 2026-10-14, REJECTED.** It hallucinated
  sponsor reads, missed real ads, produced garbled timestamps, and was
  too slow and memory-hungry. Do not re-add without a fresh evaluation.

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
  apps' description display. Lore's original Libsyn HTML descriptions stay
  untouched; only the prepended summary is plain text.
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
