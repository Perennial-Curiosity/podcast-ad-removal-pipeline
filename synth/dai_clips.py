"""Extract individual produced/DAI ad clips from the real-audio DAI diff set.

Input (read-only): <pipeline repo>/eval_work/real_audio_gt/dai/
  manifest.json (show, genre per episode), ad_clips/<slug>_{A,B}_slotNN_subst_S-E.mp3
  = stretches that differed between two downloads of the same episode.
Steps:
  1. Whisper transcript per slot clip (pipeline transcribe_chunked, cached
     in data/transcripts/dai_raw/<clip>/).
  2. Split multi-ad breaks at sponsor opening cues ("This message comes
     from", "Support for ... comes from", "sponsored by", "brought to you
     by", ...) that follow a pause/sentence end. Cut point = middle of the
     word gap before the cue.
  3. Brand from the cue ("comes from X") or the most-spoken "<name>.com".
  4. Trim leading/trailing silence (keeps <=150 ms pad), drop pieces < 12 s,
     flag pieces whose clip edge cuts a sentence (truncated_start/end).
  5. Dedupe: same brand and word-sequence similarity >= 0.6, or similarity
     >= 0.8 regardless of brand -> keep the longest copy.
Output: data/ads/dai/<id>.wav, data/ads/dai_clips.json; appends nothing to
batch1's clips.json (splice.py --pool reads both).
"""
import difflib, json, os, re, subprocess, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipe import load_pipeline
from transcribe import transcript_for

DAI = os.environ.get("PAD_DAI", "/workspace/podcast-ad-removal-pipeline/eval_work/real_audio_gt/dai")
DATA = os.environ.get("PAD_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
CUE = re.compile(
    r"^(this (message|episode|podcast|show|segment) (comes|is (sponsored|brought to you|supported|presented)|was brought)"
    r"|support for (this|npr|the|our|[a-z']+)"
    r"|(this episode|today's episode|this show) is (sponsored|brought|supported|presented)"
    r"|(brought|presented) to you by|sponsored by|today's sponsor|our sponsor"
    r"|support (also )?comes from|and (by|now) a word from)", re.I)
BRAND_AFTER = re.compile(
    r"(?:comes? from|sponsored by|brought to you by|presented by|supported by|message from)\s+"
    r"(?:our (?:friends|sponsor)s? (?:at )?)?([A-Z][\w&'\-\.]*(?:\s+[A-Z][\w&'\-\.]*){0,2})")
STOP = {"The", "This", "Our", "NPR", "And", "A", "An"}


def sentences_start(words, i):
    if i == 0:
        return True
    prev = words[i - 1][2].strip()
    gap = words[i][0] - words[i - 1][1]
    return prev.endswith((".", "?", "!")) or gap >= 0.25


GAP_SPLIT = 0.8      # s; produced spots without a spoken cue
MIN_PIECE = 12.0


def split_points(words):
    pts = []
    for i in range(1, len(words)):
        if not sentences_start(words, i):
            continue
        tail = " ".join(w[2].strip() for w in words[i:i + 12])
        if CUE.match(tail):
            pts.append(i)
    # long pauses at a sentence boundary also split (both sides >= MIN_PIECE)
    for i in range(1, len(words)):
        gap = words[i][0] - words[i - 1][1]
        if gap >= GAP_SPLIT and words[i - 1][2].strip().endswith((".", "?", "!")) and i not in pts:
            left = max([p for p in pts if p < i], default=0)
            right = min([p for p in pts if p > i], default=len(words))
            if words[i][0] - words[left][0] >= MIN_PIECE and words[right - 1][1] - words[i][0] >= MIN_PIECE:
                pts.append(i)
    return sorted(pts)


EN_STOP = {"the", "and", "to", "you", "a", "of", "for", "your", "is", "with", "at", "on", "it", "that"}


def is_english(text):
    t = re.findall(r"[a-z']+", text.lower())
    if not t:
        return False
    en = sum(1 for x in t if x in EN_STOP)
    es = sum(1 for x in t if x in ES_STOP)
    return en / len(t) >= 0.07 and en > es and sum(ord(c) > 0x2000 for c in text) < 5


ES_STOP = {"el", "la", "que", "para", "de", "en", "y", "los", "las", "con", "es", "por", "un", "una", "del"}


def brand_of(words):
    text = " ".join(w[2].strip() for w in words)
    m = BRAND_AFTER.search(text[:400])
    if m:
        toks = []
        for t in m.group(1).split():
            if t in STOP or t.lower() in ("with", "offering", "which"):
                break
            toks.append(t)
            if t.endswith((".", ",")) and not re.search(r"\.\w", t):
                break
        if toks:
            return " ".join(toks).strip(".,")
    doms = re.findall(r"([A-Za-z][\w\-]{2,})\s?\.\s?(com|org|net|io|co)\b", text)
    doms = [d[0].lower() for d in doms if d[0].lower() not in ("npr", "www")]
    if doms:
        return Counter(doms).most_common(1)[0][0] + ".com"
    if re.search(r"coca\W{0,3}cola", text, re.I):
        return "Coca-Cola"
    # fallback: a capitalized name spoken >= 2 times (not sentence-initial filler)
    caps = re.findall(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z'\-]{3,})", text)
    caps = [c.strip("'") for c in caps if c not in COMMON]
    if caps:
        c, n = Counter(caps).most_common(1)[0]
        if n >= 2:
            return c
    return None


COMMON = {"This", "That", "With", "Your", "What", "When", "Whether", "There", "Terms", "Learn",
          "Visit", "Download", "Sign", "Support", "Apply", "Details", "Holiday", "Here", "They",
          "Today", "Some", "Most", "Okay", "Just", "Hey", "Right", "Now", "Then", "Every", "Christmas",
          "Black", "Friday", "Finally", "Enjoy", "Listen", "America", "Navidad", "Quiere"}


def silence_trim(path, t0, t1):
    """Return (t0', t1') inside [t0,t1] trimmed of leading/trailing silence."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", path,
                        "-af", "silencedetect=noise=-45dB:d=0.15", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    st = [float(x) for x in re.findall(r"silence_start: ([\-\d.]+)", r)]
    en = [float(x) for x in re.findall(r"silence_end: ([\-\d.]+)", r)]
    a, b = 0.0, t1 - t0
    if st and st[0] <= 0.05 and en:
        a = max(0.0, en[0] - 0.15)
    if st and (len(en) < len(st) or en[-1] >= b - 0.05):
        b = min(b, st[-1] + 0.15)
    return t0 + a, t0 + max(a + 1, b)


def main():
    man = {e["id"]: e for e in json.load(open(os.path.join(DAI, "manifest.json")))["episodes"]}
    clip_dir = os.path.join(DAI, "ad_clips")
    out_dir = os.path.join(DATA, "ads", "dai")
    os.makedirs(out_dir, exist_ok=True)
    mod = load_pipeline(); mod.log = lambda m: None
    pieces = []
    for fn in sorted(os.listdir(clip_dir)):
        if not fn.endswith(".mp3"):
            continue
        base = fn[:-4]
        m = re.match(r"(.+)_([AB])_slot(\d+)_subst_(\d+)-(\d+)$", base)
        slug, copy, slot = m.group(1), m.group(2), int(m.group(3))
        ep = man.get(slug, {})
        path = os.path.join(clip_dir, fn)
        words, dur = transcript_for(mod, path, os.path.join(DATA, "transcripts", "dai_raw", base))
        words = [tuple(w) for w in words]
        if not words:
            continue
        cuts = [0.0] + [(words[i - 1][1] + words[i][0]) / 2 for i in split_points(words)] + [dur]
        for k in range(len(cuts) - 1):
            a, b = cuts[k], cuts[k + 1]
            ws = [w for w in words if a <= w[0] < b]
            if not ws:
                continue
            a2, b2 = silence_trim(path, a, b)
            if b2 - a2 < 12:
                continue
            text = " ".join(w[2].strip() for w in ws)
            first, last = ws[0][2].strip(), ws[-1][2].strip()
            pieces.append({
                "slug": slug, "copy": copy, "slot": slot, "piece": k, "n_pieces": len(cuts) - 1,
                "src_path": path, "src_start": round(a2, 3), "src_end": round(b2, 3),
                "duration": round(b2 - a2, 3), "show": ep.get("show") or slug, "genre": ep.get("genre"),
                "brand": brand_of(ws), "text": text, "english": is_english(text),
                "truncated_start": bool(k == 0 and first[:1].islower()),
                "truncated_end": bool(k == len(cuts) - 2 and not last.endswith((".", "?", "!"))),
            })
    # dedupe
    pieces.sort(key=lambda p: (-p["duration"]))
    kept = []
    for p in pieces:
        p["text_norm"] = re.findall(r"[a-z0-9]+", p["text"].lower())
        p["text_norm"] = re.findall(r"[a-z0-9]+", p["text"].lower())
        dup = None
        for q in kept:
            qw = q["text_norm"]
            sm = difflib.SequenceMatcher(None, p["text_norm"], qw, autojunk=False)
            r = sum(b.size for b in sm.get_matching_blocks()) / max(1, min(len(p["text_norm"]), len(qw)))
            same_brand = p["brand"] and q["brand"] and p["brand"].lower() == q["brand"].lower()
            if r >= 0.8 or (same_brand and r >= 0.6):
                dup = q; break
        if dup:
            dup.setdefault("duplicates", []).append(f"{p['slug']}_{p['copy']}_slot{p['slot']:02d}_p{p['piece']}")
            continue
        kept.append(p)
    clips = []
    for p in sorted(kept, key=lambda p: (p["genre"] or "", p["slug"], p["copy"], p["slot"], p["piece"])):
        cid = f"dai-{p['slug'][:40]}-{p['copy']}{p['slot']:02d}p{p['piece']}"
        wav = os.path.join(out_dir, cid + ".wav")
        if not os.path.exists(wav):
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{p['src_start']:.3f}", "-t", f"{p['duration']:.3f}",
                            "-i", p["src_path"], "-ar", "44100", "-ac", "1", wav], check=True)
        clips.append({"id": cid, "source": "dai", "source_show": p["show"], "genre": p["genre"],
                      "brand": p["brand"] or f"unknown-{cid}", "brand_identified": bool(p["brand"]),
                      "type": "produced (DAI fill)", "boundary_source": "A/B download diff + cue split + silence trim",
                      "episode_slug": p["slug"], "copy": p["copy"], "slot": p["slot"], "piece": p["piece"],
                      "src_path": p["src_path"], "src_start": p["src_start"], "src_end": p["src_end"],
                      "duration": p["duration"], "truncated_start": p["truncated_start"],
                      "truncated_end": p["truncated_end"], "english": p["english"],
                      "splice_ok": bool(p["english"] and not p["truncated_start"] and not p["truncated_end"]),
                      "duplicates": p.get("duplicates", []),
                      "first_words": " ".join(p["text"].split()[:14]), "last_words": " ".join(p["text"].split()[-14:]),
                      "path": wav})
    json.dump(clips, open(os.path.join(DATA, "ads", "dai_clips.json"), "w"), indent=1)
    print("pieces", len(pieces), "kept", len(clips))
    for c in clips:
        print(f"{c['genre']:11s} {c['brand'][:28]:28s} {c['duration']:6.1f}s {'TS' if c['truncated_start'] else '  '}{'TE' if c['truncated_end'] else '  '}{'  ' if c['english'] else 'NE'} dup{len(c['duplicates'])} | {c['first_words'][:70]} ... {c['last_words'][-60:]}")


if __name__ == "__main__":
    main()
