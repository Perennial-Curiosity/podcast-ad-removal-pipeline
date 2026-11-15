import json, glob, sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v4v_cut as V
d = sys.argv[1]
words = []
for p in sorted(glob.glob(os.path.join(d, "chunk_*.json"))):
    words += [tuple(w) for w in json.load(open(p))["words"]]
dur = words[-1][1]
wts = V.word_weights(words)
grid, sc = V.window_scores(words, wts, dur)
segs, _ = V.find_segments(words, dur)
print("dur", dur/60, "segs", [(round(s['start']/60,1), round(s['end']/60,1), s['peak_score']) for s in segs])
for m in range(0, int(dur // 60) + 1):
    mask = (grid >= m*60) & (grid < m*60+60)
    mx = sc[mask].max() if mask.any() else 0
    bar = "#" * int(mx)
    if len(sys.argv) > 2 or mx >= 3:
        txt = V.text_between(words, m*60, m*60+60, 18)
        print(f"{m:4d} {mx:5.1f} {bar[:40]:40s} {txt[:110]}")
