"""Sanity-check ad clips: the brand must be spoken in the clip transcript;
record first/last words so edges can be spot-checked."""
import json, os, re, sys
DATA = os.environ.get("PAD_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
clips = json.load(open(os.path.join(DATA, "ads", "clips.json")))
out = {}
for c in clips:
    T = json.load(open(os.path.join(DATA, "transcripts", "clips", c["id"], "transcript.json")))
    words = [w[2].strip() for w in T["words"]]
    text = re.sub(r"[^a-z0-9 ]", "", " ".join(words).lower())
    toks = [t for t in re.split(r"[^a-z0-9]+", c["brand"].lower()) if len(t) >= 3 and t not in ("the", "and", "com")]
    squash = text.replace(" ", "")
    hit = any(t in text.split() or t in squash for t in toks) if toks else False
    out[c["id"]] = {"ok": bool(hit), "brand": c["brand"], "n_words": len(words),
                    "first": " ".join(words[:12]), "last": " ".join(words[-12:]),
                    "duration": c["duration"]}
json.dump(out, open(os.path.join(DATA, "ads", "clip_quality.json"), "w"), indent=1)
bad = [k for k, v in out.items() if not v["ok"]]
print("clips", len(out), "brand spoken", len(out) - len(bad), "flagged", bad)
for k in list(out)[:6] + bad:
    v = out[k]; print(f"- {k} [{v['brand']}] {v['duration']}s\n    > {v['first']}\n    < {v['last']}")
