# Spliced-ad test set (real ads into real ad-free V4V podcasts)

Local tooling on branch `grok/synth-dataset` (worktree `/workspace/pad-dataset`,
forked from `grok/boundary-work` @ d5f9015). Nothing here is pushed.
Data lives in `../data/` (not committed; ~6-10 GB).

## Pipeline (regenerate in order)

All scripts use `PAD_DATA=/workspace/pad-dataset/data` and the venv
`/workspace/pad-dataset/.venv` (faster-whisper 1.2.1, yt-dlp, numpy, mutagen).

| step | command | output |
|---|---|---|
| 1. host feeds + download | `python select_hosts.py` | `data/feeds/*.xml`, `data/hosts/*.mp3`, `data/hosts/manifest.json` |
| 2. ad clips | `python ads.py` | `data/ad_sources/*.mp3`, `data/ads/clips/*.wav`, `data/ads/clips.json` |
| 3. transcripts (cached, resumable) | `python transcribe.py data/transcripts/hosts --list=hosts.tsv` (same for clips) | `data/transcripts/{hosts,clips}/<id>/` |
| 4. clip QA | `python clip_quality.py` | `data/ads/clip_quality.json` (clips whose brand is never spoken are excluded) |
| 5. V4V CTA cut | `python v4v_cut.py` | `data/hosts_cut/<id>/{audio.wav,words.json,cut_plan.json}`, `data/v4v_cta/<id>/cta_N.{mp3,json}`, `data/v4v_cta/CUT_LOG.md` |
| 6. splice | `python splice.py --n 20 --seed 1234 --min-win 1800 --max-win 4500` | `data/spliced/batch1/<ep>/{audio.mp3,meta.json}`, `gt.json`, `gt_ads.json` |
| 7. spliced transcripts | `python tr_spliced_worker.py data/spliced/batch1 K 3 20` (K=0,1,2) | `data/transcripts/spliced/<ep>/` |
| 8. baseline | `python eval_synth.py spliced --label base [--oracle]`; `python eval_synth.py v4v --label base` | `data/spliced/batch1/results/`, `data/v4v_cta/results/` |

Transcription uses the pipeline's own `transcribe_chunked` (Whisper base,
int8 CPU, 180 s chunks), so transcripts match what the pipeline would see.
The pipeline under test is imported read-only from this worktree's
`pipeline/run_pipeline.py` (override with `PAD_PIPELINE=/path/run_pipeline.py`).
Splicing is deterministic for a given seed + inputs.

## Sources

**Hosts (ad-free, Value-4-Value)** — public RSS, each feed verified to carry
`<podcast:value>` tags: No Agenda, Podcasting 2.0, Moe Factz, Into the
Doerfel-Verse, Everything's an Argument, Fun Fact Friday, Value 4 Value
(Kyrin), V4V Roundtable, Podcasting 4 Value, Bowl After Bowl. 16 episodes.
Feed URLs in `feeds.py`.

**Ads** — publisher chapter markers titled `Sponsor: <brand>`:
Changelog / Practical AI / JS Party (`podcast:chapters` JSON) and ATP (ID3
CHAP frames in the MP3). Clip = sponsor chapter start → chapter end.
BLAB was not usable: its audio is YouTube-only and YouTube answers this
box with "Sign in to confirm you're not a bot" / HTTP 429 (tried yt-dlp
with several player clients and a JS runtime; cookies were not used).

## V4V CTA removal

`v4v_cut.py` scores a 60 s sliding window of the Whisper transcript for
CTA vocabulary (donation, boost/boostagram, sats, V4V / value for value,
Time Talent Treasure, executive/associate producer, Sir/Dame/Baron titles),
spoken amounts ("$50", "33,333 sats") and donor-note cadence ("says",
"thank you", "from …"). Hysteresis (seed ≥ 9, extend while ≥ 4), merge
gaps < 4 min, pad 30 s each side, then move each edge further OUT to the
next pause. Cuts within 60 s of the episode start/end extend to the edge.
Deliberately over-inclusive (owner: "if unsure, cut more"). The cut
pieces are saved as a hard-negative set (audio + transcript + times).
Spot-check `data/v4v_cta/CUT_LOG.md` (first/last lines + top cues per cut).

## Ground truth format

`gt.json`: `{episode: {"ads": [[start_s, end_s, label], ...]}}` — the
"generic" format `eval/score_segments.py` loads. Back-to-back ads (pods)
are merged into one span; `gt_ads.json` lists every clip separately with
slot type (pre / post / pause / mid_sentence), crossfade and gap ms,
source and brand. Times are exact in the output timeline: start = first
sample with ad audio (start of fade-in overlap), end = last sample with
ad audio. Verified sample-accurate after MP3 encode/decode (offset 0.02 ms).

## Known artifacts / limitations

- **No host segues.** Spliced ads look like dynamic insertion, not
  host-read integrations ("speaking of sleep…"). Boundary results here
  are optimistic for host-read ads and closest to DAI ads.
- **Voice/room change at every splice.** Ad clips are other shows' hosts
  in other rooms; voice/reverb detectors get an easier signal than on a
  real host-read ad. Loudness is matched (EBU R128 two-pass linear
  loudnorm to the host's integrated loudness) but timbre/noise floor is not.
- **Clip edges are publisher-chapter precision.** A clip may carry a
  second of the source show's content or miss a stinger. Ground truth is
  still exact for what was inserted.
- **Ad pool is narrow:** tech-show sponsors (WorkOS, Fly.io, Squarespace…),
  mostly host-read by Changelog/ATP hosts; few produced spots. Some brands
  repeat across episodes (not within one).
- **Cut junctions.** Removing V4V segments joins host audio with a 20 ms
  fade; these joins are unlabeled discontinuities a detector could flag.
- **Over-cutting on V4V-topic shows.** Value 4 Value, V4V Roundtable and
  Podcasting 4 Value talk about V4V as their subject, so large parts were
  cut. Content loss is harmless for GT, but those hosts contribute less.
- **No chapters, no episode history**, and (default) the description does
  not list the inserted brands, like DAI. `--oracle` adds a "Sponsors:"
  line with the inserted brands, like a host-read show that lists sponsors.
- Whisper base only; voice/reverb methods run on the spliced MP3.

## Batch 1 results (2026-11-15, pipeline grok/boundary-work @ d5f9015)

Set: 20 spliced episodes (20–82 min, 1,080 min total) from 16 hosts,
84 inserted ads (71 GT spans after merging pods; slots: 42 pause,
30 mid-sentence, 8 post-roll, 4 pre-roll), 44 distinct clips used.
V4V cut: 317 of 1,396 host minutes (per-host table in `data/v4v_cta/summary.json`).

| condition | Seg P | Seg R | Seg F1 | Time P | Time R | median edge err | ≤5s edges |
|---|---|---|---|---|---|---|---|
| description only (DAI-like) | 27.1% (13/48) | 18.3% (13/71) | 21.8% | 95.0% | 24.3% | 5.20s (26 edges) | 46.2% |
| `--oracle` sponsors in description | 59.4% (38/64) | 53.5% (38/71) | 56.3% | 93.0% | 53.3% | 5.22s (76 edges) | 46.1% |

Only 2 predictions (112 s) fell entirely on host content in either
condition; most segment "false positives" are fragments/partial blocks
inside real ads that miss IoU 0.5 (base: 40 of 71 spans touched by some
prediction, oracle: 58 of 71). Start edges are biased late
(signed mean +16.6 s base, +4.5 s oracle).

V4V false positives (full uncut hosts, 16 episodes, `data/v4v_cta/results/base_v4v_fp.json`):
524 s predicted inside 19,034 s of cut CTA (2.8%), 9 of 35 cut segments
touched; 1,160 s predicted in 64,705 s of the remaining content (1.8%),
18 blocks ≈ 1.0 per clean hour (mostly duplicate/reverb/voice labels
like `dry-island`, i.e. audio-method hits on music/bits).
