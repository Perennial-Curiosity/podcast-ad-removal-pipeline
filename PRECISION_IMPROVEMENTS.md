# Precision improvements to ad detection (Grok)

**Author:** Grok `<grok@x.ai>`  
**Branch:** `grok/precision-heatmap-v1`

## Apply the code changes

```bash
git checkout grok/precision-heatmap-v1
git apply precision-improvements-heatmap-v1.patch
# verify
python3 -c "import ast; ast.parse(open('pipeline/run_pipeline.py').read()); print('OK')"
```

## What changed

1. **Weighted heat map** — `METHOD_WEIGHTS` + `HEATMAP_SCORE_THRESHOLD = 1.15` replace integer ≥2 votes
2. **Single-method floors** — chapter 20s, brand/cta 30s, cluster 45s
3. **Variant danger scoring** — short/common tokens gated in `find_mentions`
4. **URL tiering** — `url` (0.70) vs `url-strong` (0.85)
5. **Duplicate tightening** — suppress segments with Jaccard > 0.55 vs episode head

## Validation still required before production

- Unsubscribe 286 / 287 (owner ground truth)
- Security Now 1101 regression
- 25-episode SponsorBlock set (recall, median edge error, within-1s, false blocks)

Do not merge solely on green CI; run the regression checklist in the proposal.
