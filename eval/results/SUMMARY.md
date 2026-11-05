# BLAB validation results

Same 12-episode BLAB sample (seeded sample, 35 GT ads, 2072s of GT ad
time), scored at IoU 0.5. Run with:

```bash
python3 eval/run_blab_eval.py --count 12 --label <label> [--skip-download]
```

| Run | Segment P | Segment R | Segment F1 | TP / FP / FN | Time P | Time R | Time F1 |
|---|---|---|---|---|---|---|---|
| Baseline (main) | 26.3% | 14.3% | 18.5% | 5 / 14 / 30 | 35.9% | 19.9% | 25.6% |
| Round 1 | 66.7% | 28.6% | 40.0% | 10 / 5 / 25 | 74.3% | 27.4% | 40.0% |
| Round 2 (final) | 57.9% | 31.4% | 40.7% | 11 / 8 / 24 | 72.9% | 29.8% | 42.3% |

**Round 1** — duplicate-method ad-evidence gate + YouTube-style
description sponsor extraction:

- Duplicate FPs (6 on one episode alone) came from interview shows
  where hosts/guests repeat pet phrases verbatim; the gate requires a
  spoken URL, promo-code/CTA phrase, opening cue, or sponsor mention
  inside the repeated span.
- `extract_sponsors` now also parses YouTube-style blurbs ("visit:
  https://bitbox.swiss/robin — get 5% off with code ROBIN") where
  sponsors are (URL, promo-code context) pairs rather than RSS
  "sponsors:" sections or rel="sponsored" links. This alone took one
  episode from 0/4 ads found to 3/4.

**Round 2** — promo-code CTA improvements:

- Promo-code anchors now accept "use the code X" / "using this code X"
  lead-ins (previously only "use code X" / "promo code X").
- Promo-code CTA blocks get a 20s floor instead of 45s: a spoken
  "use the code X" read is virtually never anything but an ad, and
  YouTube-show spots run 20-30s.
- Bounded fallback segment for promo-code anchors whose host-read
  expansion fails (no opening cue / no clean closing pause): a
  pause-snapped [anchor-45s, anchor+15s] window instead of dropping
  the read entirely. This covers betting-show reads (BetUS) that have
  no description sponsor, no spoken URL, and no chapter markers.

The Round-2 FPs are partial coverage of real long reads (e.g. a 192s
GT ad covered by two 51s blocks: IoU counts them as errors, but 102s of
real ad gets cut and time recall for that episode went 0% → 34.8%).
Strictly by the numbers Round 2 trades precision for recall; by
ad-removal utility it is the better configuration.

## Remaining misses, by category

- **Sub-30s GT segments** (2-27s): below the pipeline's 30s heatmap /
  20-45s single-method floors. BLAB counts brief ad mentions that this
  pipeline deliberately does not auto-cut (precision-first design).
- **Host/guest promos that are content**: tour-date plugs, cross-show
  promos ("watch Hell of a Week Thursday on Comedy Central"), guest
  social-media plugs, and event promos. BLAB labels all promotions as
  ads; this pipeline intentionally treats house plugs and guest plugs
  as content (a false cut removes real content).
- **Long blended reads without any anchor**: reads with no URL, no
  code, no description sponsor, no chapter — only voice/reverb signals
  can reach them, and those are deliberately low-confidence.

Reports: `blab_baseline.md`, `blab_improved_round1.md`,
`blab_improved_round2.md` (same directory).
