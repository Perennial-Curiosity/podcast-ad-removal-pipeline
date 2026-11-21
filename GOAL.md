# AdChisel Reference

## End goal

Label every ad in a podcast episode — its exact beginning and its exact end.

- **Precision required: within 1 second on each edge.** 1–2 seconds is acceptable. More is a miss.
- Nothing is cut. The original audio is published whole.
- Ads are marked as chapters the listener can skip.

That precision is the goal. Finding the ad is only step one.

## How it works

1. **Download** — Get the episode exactly as published.

2. **Transcribe** — Convert speech to text, with a time for every word.

3. **Detect** — Several independent methods scan the transcript, each voting second by second:
   - Brand matcher — sponsor names from the episode description
   - Call-to-action hunter — "use code…", URLs, discount offers
   - Cluster finder — many brand mentions packed into one minute
   - Duplicate scanner — the same 15+ seconds repeated in one episode
   - Chapter scan — publisher chapters titled Ad, Sponsor, or Commercial
   - Voice anomaly — logged for review only; it never marks an ad alone

   Two or more methods agreeing is high confidence. Some strong single methods also count.

   On feeds that serve different ads per download, a second pass downloads the episode again and compares the two transcripts: the regions that differ are inserted ads. A differing region is only marked when ad evidence backs it. Where a slot overlaps a detected block, the slot's edges replace the detector's edges. If the feed already supplies a transcript, no second download is made; the publisher transcript is the comparison instead.

   Eight more mechanisms, merged 2026-11-14:
   - Short brand names need a word boundary — "aura" no longer matches "restaurant", "stop" no longer matches "stopping".
   - Cross-episode recurrence — a sponsor that appears in the show's other episodes backs up a block.
   - Publisher-transcript gaps — where the publisher supplies a transcript, the parts it leaves out are ad evidence; each gap is trimmed to the first and last ad word.
   - Partial-overlap slots merge into the detected block instead of standing alone — this removes the false slot that used to appear inside a real ad.
   - Back-to-back split — when two ads run with no gap between them, the second one is split out of the fused block.
   - Density floor — a differing region is only marked when at least half its words differ between the two downloads; this removes a non-ad outro tail and keeps every real ad.
   - URL-anchored split — when the second ad's brand name cannot be heard (the transcript mishears it), the website address the host reads out loud can anchor the split instead.
   - Per-podcast history — the program keeps a file per podcast of every ad it has found (position, duration, sponsor, dynamic or host-read); once a show has 5 episodes on file, the usual position of its opening ad votes too, with a guard that stops the vote from gluing two different ads together.

4. **Refine the edges** — Move each start and end inward to the nearest real evidence: the first and last brand mention, call to action, or URL inside the block. Starts stay where the opening cue is — cues come before brand names. Each edge also gets a confidence score (see edge sharpness below).

5. **Name the sponsor** — Attach the company or product the ad is for.

6. **Publish** — Original audio, unchanged, in a feed for Podcast Addict, with chapters added:
   - `Muse Detected Ad For <name>`
   - `Muse Detected Ad` (no name) — when a block's evidence is too weak to name a sponsor, the chapter carries no name rather than a made-up one
   - `Muse Detected House Plug For <name>` — see below

7. **Score** — Compare every edge against the owner's hand-marked times. Report per edge: error in seconds, share within 1 second, share within 2 seconds, median error, and worst error.

## Words that must not be confused

- **Ad** — A paid sponsor read, host-read or inserted.
- **House plug** — The show promoting itself or its own products, including Patreon reads and Pepperbox. Labeled as a house plug, never as an ad.
- **Recall** — The share of real ads that were found. 9 of 9 ads found is 100% recall.
- **Edge error** — How far a marked start or end is from the true start or end, in seconds.
- **Edge sharpness (boundary confidence)** — How sharply the evidence votes change at an edge: the average votes just inside minus the average just outside. Below 0.5 the edge is "flat" and likely wrong — flat edges miss by 14.3 seconds on average, sharp ones by 2.7. It flags shaky edges; it cannot fix them, because the true boundary is usually flat too.
- **False block** — A chapter marking something that is not an ad.

## Why it is built this way

- **Mark, don't cut** — Inserted ads change between downloads. Cutting would publish different audio than the listener expects, and a wrong cut removes real content.
- **Many methods, not one** — No single signal catches every ad. Agreement between independent methods is what makes a mark trustworthy.
- **Edges come from evidence, not word accuracy** — Tested 2026-11-10: a far more accurate transcript did not improve edges, and sometimes worsened them, because it also heard real brand mentions outside the ad.
- **Generic and automatic** — One system for many shows and episodes. No hand-tuning per episode.
- **The owner's markings are the truth** — Every test is scored against them, never against another system's opinion.

## Current status

- Automatic marking is **paused**. The owner is marking new episodes by hand.
- Dynamic-slot comparison integrated 2026-11-11: on feeds that serve different ads per download, a second download is transcribed and compared with the first; differing regions are inserted ads, marked only when ad evidence backs them. On the owner's two marked episodes it raised coverage at the strict scorer from 7/9 to 9/9 ads (episode 286) and 6/8 to 7/8 (episode 287), with dynamic-ad edges averaging 0.42 seconds of error. The false slot inside a real ad on 286 was fixed 2026-11-13 by tightening the gate: a slot overlapping a detected block by under half now merges into the block.
- Six-mechanism merge live 2026-11-14: the substring guard, cross-episode recurrence, publisher-transcript differential, gate tightening, back-to-back split, and density floor are now in the pipeline. On the 25-episode test set: 75 of 107 ads found, 38 false blocks, median edge error 4.8 seconds, 26.0% of edges within 1 second. On the owner's two marked episodes, slot-flow scoring is 9/9 ads on 286 with no false blocks and 7/8 on 287 with one false block (the Pepperbox house plug).
- Fusion-fix merge 2026-11-14: the URL-anchored split rescue and the per-podcast history voter (with a guard against gluing ads together) are now live. Test set now: 75 of 107 ads found, 40 false blocks, median edge error 4.8 seconds, 26.0% of edges within 1 second. The two extra false blocks are both real second ads the test's answer key cannot see (Our Place and Rorra), so the count is a measurement artifact, not new mistakes.
- Label-fix merge 2026-11-18: three label rules are now live — a block with only weak evidence gets a plain chapter with no sponsor name instead of a made-up one, a name that is really the show's own identity is stripped the same way, and the missed-sponsors check no longer flags a sponsor a block already names. Test set unchanged: 75 of 107 ads found, 40 false blocks, median edge error 4.8 seconds, 26.0% of edges within 1 second, with five wrong names removed and no detection change.
- Full-pipeline re-measurement 2026-11-18: the whole pipeline as actually run was scored on the 25-episode test set — 75 of 107 ads found, 47 counted false blocks, median edge error 4.8 seconds, 26.0% of edges within 1 second. After hand-checking the extras, 35 are true false blocks; the other 12 are real ads the test's answer key cannot see (inserted ads and second ads of merged pairs).
- False-block adjudication 2026-11-18: all 35 counted false blocks were hand-checked against the transcripts — 32 are real paid ads the test's answer key cannot see, 2 are the show promoting itself, and exactly 1 is a genuine mistake (a Security Now segment about a PHP vulnerability). The pipeline as run marks 118 real paid ads plus 3 self-promotional blocks and 1 true false block, out of 122 blocks.
- Adjudicated score 2026-11-18: scored against the full truth of 150 known real ads (the test's 107 labels plus 43 real ads it cannot see), the pipeline finds 118 — true recall 78.7% — with 1 true false block (4 counting self-promotion as false).
- Slot-determinism fix MERGED 2026-11-19: keeping the second download instead of fetching a fresh one each run, plus ignoring reverb-only evidence at the slot gate, makes slot results identical run to run with no detection change. Verified on the frozen test set after the merge — every headline identical to before. The 102:41 start on episode 289 was still about 26 seconds early after this merge — a separate edge problem, now fixed by a held patch (see below).
- Slot-start trim MERGED 2026-11-19: when a slot's head is the same short phrase repeated three or more times in a row, the start moves past the last full repeat. On episode 289 it moves the 102:41 start from 6179.0 to 6204.9 — 0.4 seconds before the ad copy starts, inside the 1-second goal — with no change anywhere else on the test set. Post-merge verification on the frozen test set reproduces every headline exactly.
- Ep 289 re-run 2026-11-19, publish held: the re-run with the merged pipeline completed in about 33 minutes, but Megaphone served a truncated second download — 5,496.8 seconds against the episode's 6,229.4 — so the duration guard rejected all slots and the re-run produced 10 detector-only blocks. The publish gate failed as designed and nothing was published; the published file is still the 10-block version. The blocker is the truncated download, not the pipeline — a plain re-run would reuse the truncated copy, so the pinned file has to be deleted first for a fresh attempt.
- Ep 289 published 2026-11-19 with the corrected slots: after the pinned truncated file was deleted, a fresh second download (6,129.5 seconds) produced 11 blocks — the 102:41 chapter starts at 6205.2, within 0.1 seconds of the ad copy start, and there is no chapter near 98:45. One correction found during verification: the re-run reused the early part of the old truncated download's transcript, because deleting the marker file defeats the cache check — the 102:41 chapter is in the freshly transcribed tail and is correct, but the four earlier merged slots' edges are not yet verified against the fresh download. The fix is to make the cache check drop the old transcript when the marker is missing, then re-run cleanly and re-publish if the blocks differ.
- Ep 289 clean re-run 2026-11-19, no re-publish: the cache check was fixed and the second download was fully re-transcribed, but the run was confounded — a new download check deleted the good cached episode audio because the host rotates the episode on nearly every request, so detection ran on old words over new audio and produced 13 blocks, 3 of them false, with no 102:41 chapter. The published 11-block file stands and is verified unchanged. The download check was reverted the same day. Two open items: the episode transcript cache needs the same identity check as the second-download cache, and the episode's state now lists the 13 confounded blocks against the 11-block published file.
- Full pipeline audit 2026-11-19, analysis only, no fixes: three teams read the pipeline end to end and found 3 critical, 37 major, 21 minor — 61 findings. Critical: publish saves state before the feed push, so a failed feed push leaves the episode marked published and never selected again; edge refinement can shrink a block far below the 30-second and 45-second floors with no re-check; one slot overlapping two detector blocks collapses both into a single block and the second ad disappears. The episode transcript cache has no identity check, and two more caches share that gap.
- Fix round for all 61 findings, applied 2026-11-19/20: 60 fixed, 1 invalid as proposed (the density "mismatch" turned out to be the calibrated discriminator, so no behaviour change). The test set improved: detector recall 0.701 to 0.757 (75 to 81 of 107), false positives 40 to 37, edge accuracy unchanged. The episode transcript cache now has an identity sidecar, and 289's state blocks were restored to the published 11.
- Re-audit after the fix round, 2026-11-19/20, analysis only: 54 of the original 61 fixed, 7 partially fixed, 0 not fixed. Remaining: 1 critical, 7 major, 22 minor — 30 total. Critical: generic label parts (`ad`, `dry-island`) collide as sponsor identity in the slot merge and the heat-map gap merge, deleting a whole ad. The test-set numbers were independently re-measured and match exactly (detector 0.757, false positives 37).
- Fix round for the re-audit's 1 critical and 7 major, applied 2026-11-20: 8 fixed, 0 held. Generic label parts no longer serve as ad identity; the feed script no longer claims the feed is pushed after a local-only regeneration; asset reuse now requires a matching SHA-256. Edge accuracy improved slightly (within-1s 25.9% to 26.5%).
- Fix round for the re-audit's 22 minor, applied 2026-11-20: 21 fixed, 1 held. The held one is T2-N7 — the audio-duration detection horizon is correct in design but adds one false block on the test set (Leo's sign-off cross-promo for a sister show), so the patch is preserved, not applied. The test set is otherwise identical on all four paths, and the stale 289 driver that could have clobbered the repaired state is deleted.
- T2-N7 applied with the cross-promo guard, 2026-11-20: the held horizon patch is live together with a generic content gate — a block that only reaches the floor because of the audio horizon is marked only if a content method (CTA, brand, cluster, duplicate, chapter, brand-orphan) voted in it. The test set is identical on all four paths, and the cross-promo block is gone. All 61 original findings and all 30 re-audit findings are now resolved.
- Content-length check added 2026-11-19: downloads now compare the bytes received against the size the server advertised, and fail loudly on a short read instead of passing silently. Tested 12 for 12 on truncated downloads with no change anywhere else on the test set. It only catches truncation when the server advertises a larger size than it sends; the duration guard remains the backstop.
- Transcription model: still the fast base model. A larger model transcribes more accurately but does not improve edges; a hotwords switch was approved, then abandoned after three failed verifications.
- Every marked edge now carries a sharpness score (added 2026-11-10). Moving flat edges to the nearest vote step was tested the same day and discarded — it pushed good edges out of place.

*Update this page whenever the goal, a step, or a definition changes — before the change is treated as settled.*
