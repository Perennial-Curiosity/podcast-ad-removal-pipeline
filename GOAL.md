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

4. **Refine the edges** — Move each start and end inward to the nearest real evidence: the first and last brand mention, call to action, or URL inside the block. Starts stay where the opening cue is — cues come before brand names. Each edge also gets a confidence score (see edge sharpness below).

5. **Name the sponsor** — Attach the company or product the ad is for.

6. **Publish** — Original audio, unchanged, in a feed for Podcast Addict, with chapters added:
   - `Muse Detected Ad For <name>`
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
- Dynamic-slot comparison integrated 2026-11-11: on feeds that serve different ads per download, a second download is transcribed and compared with the first; differing regions are inserted ads, marked only when ad evidence backs them. On the owner's two marked episodes it raised coverage at the strict scorer from 7/9 to 9/9 ads (episode 286) and 6/8 to 7/8 (episode 287), with dynamic-ad edges averaging 0.42 seconds of error. One open item: a false slot inside a real ad on 286 passes the gate — the owner decides whether to accept it or tighten the gate.
- Transcription model: still the fast base model. A larger model transcribes more accurately but does not improve edges; a hotwords switch was approved, then abandoned after three failed verifications.
- Every marked edge now carries a sharpness score (added 2026-11-10). Moving flat edges to the nearest vote step was tested the same day and discarded — it pushed good edges out of place.

*Update this page whenever the goal, a step, or a definition changes — before the change is treated as settled.*
