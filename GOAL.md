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

4. **Refine the edges** — Move each start and end inward to the nearest real evidence: the first and last brand mention, call to action, or URL inside the block. Starts stay where the opening cue is — cues come before brand names.

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
- **False block** — A chapter marking something that is not an ad.

## Why it is built this way

- **Mark, don't cut** — Inserted ads change between downloads. Cutting would publish different audio than the listener expects, and a wrong cut removes real content.
- **Many methods, not one** — No single signal catches every ad. Agreement between independent methods is what makes a mark trustworthy.
- **Edges come from evidence, not word accuracy** — Tested 2026-11-10: a far more accurate transcript did not improve edges, and sometimes worsened them, because it also heard real brand mentions outside the ad.
- **Generic and automatic** — One system for many shows and episodes. No hand-tuning per episode.
- **The owner's markings are the truth** — Every test is scored against them, never against another system's opinion.

## Current status

- Automatic marking is **paused**. The owner is marking new episodes by hand.
- Best proven edge method, not yet integrated: download an episode twice and compare the two transcripts. Where they differ is an inserted ad. Mean edge error 0.43 seconds; 13 of 14 edges within 1 second. It cannot find host-read ads — those are identical in both downloads — so it would add to the detector, not replace it.
- Transcription model: still the fast base model. A larger model transcribes more accurately but does not improve edges, so no switch has been approved.

*Update this page whenever the goal, a step, or a definition changes — before the change is treated as settled.*
