# BLAB ad detection — improved

Episodes: 12  |  IoU threshold: 0.5

| Metric | Segment-based | Time-based |
|---|---|---|
| Precision | 66.7% | 74.3% |
| Recall | 28.6% | 27.4% |
| F1 | 40.0% | 40.0% |
| Counts | TP 10 / FP 5 / FN 25 | 2072s GT ad time, 764s predicted |

## Per episode

| Episode | GT ads | Predicted | Seg P | Seg R | Time P | Time R |
|---|---|---|---|---|---|---|
| ACZrZ9Bm-8I | 3 | 2 | 50.0% | 33.3% | 75.7% | 78.2% |
| 1m1ye7pTgs0 | 2 | 0 | n/a | 0.0% | n/a | 0.0% |
| OJU7BxCqdVQ | 2 | 1 | 0.0% | 0.0% | 13.1% | 57.9% |
| LXB0d_3WntM | 2 | 1 | 100.0% | 50.0% | 85.7% | 57.8% |
| KWQMS_s4lu8 | 4 | 1 | 0.0% | 0.0% | 0.0% | 0.0% |
| DcyyiWkLVHQ | 4 | 3 | 100.0% | 75.0% | 91.9% | 71.3% |
| 7WQqwB8h9aw | 4 | 1 | 100.0% | 25.0% | 98.2% | 26.8% |
| xAlaCKk2UWg | 2 | 1 | 0.0% | 0.0% | 100.0% | 13.0% |
| 712gq_OmD6Q | 4 | 1 | 100.0% | 25.0% | 96.6% | 30.2% |
| cAgxuqGMUYg | 2 | 1 | 100.0% | 50.0% | 100.0% | 42.9% |
| 1sOrZTP4pUQ | 2 | 3 | 66.7% | 100.0% | 58.6% | 82.7% |
| 1mYpuu5Wcjw | 4 | 0 | n/a | 0.0% | n/a | 0.0% |

### ACZrZ9Bm-8I
- MISS 1080s-1107s (27s) — uncovered ground truth
- MISS 1450s-1456s (6s) — uncovered ground truth
- FP 1410s-1446s (36s) — predicted, no GT overlap >= IoU

### 1m1ye7pTgs0
- MISS 1661s-1712s (51s) — uncovered ground truth
- MISS 3179s-3259s (80s) — uncovered ground truth

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
- MISS 47s-67s (20s) — uncovered ground truth
- MISS 1060s-1354s (294s) — uncovered ground truth
- MISS 2597s-2630s (33s) — uncovered ground truth
- MISS 4810s-4866s (56s) — uncovered ground truth
