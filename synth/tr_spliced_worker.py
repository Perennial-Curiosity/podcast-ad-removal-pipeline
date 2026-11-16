"""Transcribe spliced episodes as they appear. usage: tr_spliced_worker.py BATCH K N TOTAL"""
import os, sys, time, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipe import load_pipeline
from transcribe import transcript_for, spliced_tdir
batch_dir = batch = sys.argv[1]
batch, k, n, total = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
DATA = os.environ.get("PAD_DATA")
mod = load_pipeline(); mod.log = lambda m: None
done = set()
while len(done) < len([i for i in range(total) if i % n == k]):
    for d in sorted(glob.glob(os.path.join(batch, "synth-*"))):
        eid = os.path.basename(d)
        idx = int(eid.split("-")[1])
        if idx % n != k or eid in done or not os.path.exists(os.path.join(d, "meta.json")):
            continue
        t0 = time.time()
        w, dur = transcript_for(mod, os.path.join(d, "audio.mp3"), spliced_tdir(DATA, batch_dir, eid))
        done.add(eid)
        print(eid, len(w), f"{dur/60:.1f}m", f"{time.time()-t0:.0f}s", flush=True)
    time.sleep(20)
