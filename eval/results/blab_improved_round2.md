# BLAB ad detection — improved3

Episodes: 12  |  IoU threshold: 0.5

| Metric | Segment-based | Time-based |
|---|---|---|
| Precision | 57.9% | 72.9% |
| Recall | 31.4% | 29.8% |
| F1 | 40.7% | 42.3% |
| Counts | TP 11 / FP 8 / FN 24 | 2072s GT ad time, 846s predicted |

## Per episode

| Episode | GT ads | Predicted | Seg P | Seg R | Time P | Time R |
|---|---|---|---|---|---|---|
| ACZrZ9Bm-8I | 3 | 2 | 50.0% | 33.3% | 64.1% | 43.5% |
| 1m1ye7pTgs0 | 2 | 1 | 0.0% | 0.0% | 55.5% | 23.6% |
| OJU7BxCqdVQ | 2 | 1 | 0.0% | 0.0% | 13.1% | 57.9% |
| LXB0d_3WntM | 2 | 1 | 100.0% | 50.0% | 79.2% | 35.3% |
| KWQMS_s4lu8 | 4 | 3 | 0.0% | 0.0% | 76.4% | 34.8% |
| DcyyiWkLVHQ | 4 | 3 | 100.0% | 75.0% | 91.6% | 68.1% |
| 7WQqwB8h9aw | 4 | 1 | 100.0% | 25.0% | 98.2% | 26.8% |
| xAlaCKk2UWg | 2 | 1 | 0.0% | 0.0% | 100.0% | 13.0% |
| 712gq_OmD6Q | 4 | 1 | 100.0% | 25.0% | 96.6% | 30.2% |
| cAgxuqGMUYg | 2 | 1 | 100.0% | 50.0% | 100.0% | 42.9% |
| 1sOrZTP4pUQ | 2 | 3 | 66.7% | 100.0% | 51.0% | 60.8% |
| 1mYpuu5Wcjw | 4 | 1 | 100.0% | 25.0% | 66.5% | 4.9% |

### ACZrZ9Bm-8I
- MISS 1080s-1107s (27s) — uncovered ground truth
- MISS 1450s-1456s (6s) — uncovered ground truth
- FP 1410s-1446s (36s) — predicted, no GT overlap >= IoU

### 1m1ye7pTgs0
- MISS 1661s-1712s (51s) — uncovered ground truth
- MISS 3179s-3259s (80s) — uncovered ground truth
- FP 1637s-1692s (56s) — predicted, no GT overlap >= IoU

### OJU7BxCqdVQ
- MISS 931s-935s (4s) — uncovered ground truth
- MISS 1205s-1211s (6s) — uncovered ground truth
- FP 1165s-1211s (46s) — predicted, no GT overlap >= IoU

### LXB0d_3WntM
- MISS 3654s-3691s (37s) — uncovered ground truth

### KWQMS_s4lu8
- MISS 161s-163s (2s) — uncovered ground truth
- MISS 2026s-2218s (192s) — uncovered ground truth
- MISS 4515s-4563s (48s) — uncovered ground truth
- MISS 4823s-4871s (47s) — uncovered ground truth
- FP 2041s-2092s (50s) — predicted, no GT overlap >= IoU
- FP 2164s-2214s (50s) — predicted, no GT overlap >= IoU
- FP 4470s-4501s (31s) — predicted, no GT overlap >= IoU

### DcyyiWkLVHQ
- MISS 4535s-4553s (18s) — uncovered ground truth

### 7WQqwB8h9aw
- MISS 63s-81s (18s) — uncovered ground truth
- MISS 2788s-2805s (17s) — uncovered ground truth
- MISS 2907s-2927s (20s) — uncovered ground truth

### xAlaCKk2UWg
- MISS 646s-725s (79s) — uncovered ground truth
- MISS 4815s-5037s (222s) — uncovered ground truth
- FP 4972s-5011s (39s) — predicted, no GT overlap >= IoU

### 712gq_OmD6Q
- MISS 684s-704s (19s) — uncovered ground truth
- MISS 875s-903s (28s) — uncovered ground truth
- MISS 6968s-6988s (20s) — uncovered ground truth

### cAgxuqGMUYg
- MISS 3118s-3170s (52s) — uncovered ground truth

### 1sOrZTP4pUQ
- FP 234s-280s (47s) — predicted, no GT overlap >= IoU

### 1mYpuu5Wcjw
- MISS 1060s-1354s (294s) — uncovered ground truth
- MISS 2597s-2630s (33s) — uncovered ground truth
- MISS 4810s-4866s (56s) — uncovered ground truth
