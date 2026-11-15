"""Cached Whisper transcription with the pipeline's own transcribe_chunked.
usage: transcribe.py OUT_ROOT id=path [id=path ...]   (or --list file.tsv)"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipe import load_pipeline


def transcript_for(mod, audio, tdir):
    full = os.path.join(tdir, "transcript.json")
    if os.path.exists(full):
        with open(full) as f:
            d = json.load(f)
        return [tuple(w) for w in d["words"]], d["duration"]
    return mod.transcribe_chunked(audio, tdir)


def main():
    out_root = sys.argv[1]
    pairs = []
    for a in sys.argv[2:]:
        if a.startswith("--list="):
            for line in open(a[7:]):
                if line.strip():
                    i, p = line.rstrip("\n").split("\t")
                    pairs.append((i, p))
        else:
            i, p = a.split("=", 1)
            pairs.append((i, p))
    mod = load_pipeline()
    mod.log = lambda m: None
    for i, p in pairs:
        t0 = time.time()
        words, dur = transcript_for(mod, p, os.path.join(out_root, i))
        print(f"{i}: {len(words)} words, {dur/60:.1f} min audio, {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
