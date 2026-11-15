"""Find and cut Value-4-Value donation / boostagram / donor-mail segments.

Owner guidance (2026-11-15): V4V shows have 1-2 segments asking for
support ("donation", "V4V", "value", "support", "boost", "sats",
"Time, Talent, Treasure"), usually followed by a long reading of donor
notes with amounts read aloud, where fans often plug their own
show/cause/business. 5-40 min. Cut them all; if unsure, cut more.

Method (transcript only):
  1. per-word cue weights (CTA vocabulary, spoken money/sats amounts,
     donor-note cadence like "says"/"writes"/"thank you"/"Sir ..."),
  2. score = weighted cue count in a 60 s window, sampled every 5 s,
  3. windows above HI seed a segment; it grows while the score stays
     above LO (hysteresis), segments < MERGE_GAP apart are merged,
  4. each edge is padded by PAD s and then moved further out to the
     next pause >= 0.8 s (never inward) -> conservative cut,
  5. short weak segments are still cut (conservative) but flagged.
Outputs per host: cut_plan.json (kept intervals, cut segments with
scores + first/last lines), the cut host audio, and the cut segments
as separate audio + transcript (hard-negative set).
"""
import json, os, re, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WIN = 60.0
STEP = 5.0
HI = 9.0
LO = 4.0
MERGE_GAP = 240.0
PAD = 30.0
MIN_SEG = 45.0

STRONG = {  # token -> weight
    "donation": 3, "donations": 3, "donate": 3, "donated": 3, "donating": 3,
    "donor": 3, "donors": 3, "v4v": 3, "value4value": 3, "boost": 2.5,
    "boosts": 3, "boosted": 3, "boosting": 2.5, "boostagram": 3, "boostagrams": 3,
    "booster": 2, "boosters": 2, "sats": 3, "sat": 1.5, "satoshis": 3, "satoshi": 2,
    "treasure": 2, "talent": 1, "producer": 2, "producers": 2.5, "producership": 3,
    "executive": 1.5, "associate": 1, "knighted": 2, "knighthood": 2, "knight": 1,
    "paypal": 2, "patreon": 2, "fountain": 1, "podverse": 1, "castamatic": 1,
    "zap": 1.5, "zaps": 2, "zapped": 2, "lightning": 1, "streaming": 0.5, "streamed": 1,
    "support": 1, "supporting": 1, "supporters": 1.5, "contribution": 2,
    "contributions": 2, "contributed": 1.5, "dollars": 1.5, "bucks": 1.5, "euros": 1.5,
    "thousand": 0.7, "hundred": 0.5, "says": 0.7, "writes": 1, "wrote": 0.5,
    "note": 0.5, "thank": 0.7, "thanks": 0.7, "sir": 1, "dame": 1.5, "baron": 1.5,
    "baroness": 1.5, "duke": 0.7, "jingle": 0.5, "karma": 1, "dvorak.org": 3,
    "membership": 1, "members": 0.5, "split": 0.7, "splits": 1, "wallet": 0.7,
    "alby": 1.5, "ducks": 1,
}
PHRASES = [  # (regex over normalized text, weight)
    (r"value for value", 4), (r"value 4 value", 4), (r"time,? talent,? (and )?treasure", 6),
    (r"executive producers?", 3), (r"associate executive", 3), (r"support (the|our|this) (show|podcast)", 4),
    (r"\d[\d,\.]* (sats|satoshis|dollars|bucks|euros)", 3), (r"\$ ?\d", 2.5),
    (r"(hundred|thousand) (sats|dollars|bucks)", 3), (r"row of ducks", 3),
    (r"thank you (so much )?(to|for)", 1.5), (r"in the morning", 1.5),
    (r"(boost|boosted|boosts) (in|with|of)", 2), (r"(says|writes|wrote),? ", 0.8),
    (r"\bfrom (sir|dame|baron)", 2), (r"(dvorak\.org|noagenda ?donations|podcastindex\.org/donate)", 3),
    (r"(new podcast apps?|podcastapps\.com)", 2),
]


def norm(w):
    return re.sub(r"[^\w\.\$']", "", w.lower()).strip(".'")


def word_weights(words):
    toks = [norm(w[2]) for w in words]
    wts = np.array([STRONG.get(t, 0.0) for t in toks], dtype=float)
    # phrase cues: run over a joined string with char->word index
    text, idx = [], []
    for i, w in enumerate(words):
        s = w[2].strip().lower()
        text.append(s)
        idx.extend([i] * (len(s) + 1))
    joined = " ".join(text)
    for pat, wgt in PHRASES:
        for m in re.finditer(pat, joined):
            if m.start() < len(idx):
                wts[idx[m.start()]] += wgt
    return wts


def window_scores(words, wts, duration):
    t = np.array([(w[0] + w[1]) / 2 for w in words])
    grid = np.arange(0, max(duration, 1.0), STEP)
    cs = np.concatenate([[0], np.cumsum(wts)])
    lo = np.searchsorted(t, grid - WIN / 2)
    hi = np.searchsorted(t, grid + WIN / 2)
    return grid, cs[hi] - cs[lo]


def pauses(words, min_gap=0.8):
    out = []
    for a, b in zip(words, words[1:]):
        if b[0] - a[1] >= min_gap:
            out.append((a[1], b[0]))
    return out


def find_segments(words, duration):
    wts = word_weights(words)
    grid, sc = window_scores(words, wts, duration)
    segs = []
    i = 0
    n = len(grid)
    while i < n:
        if sc[i] >= HI:
            a = i
            while a > 0 and sc[a - 1] >= LO:
                a -= 1
            b = i
            while b + 1 < n and sc[b + 1] >= LO:
                b += 1
            segs.append([grid[a] - WIN / 2, grid[b] + WIN / 2, float(sc[a:b + 1].max())])
            i = b + 1
        else:
            i += 1
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] <= MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], s[1])
            merged[-1][2] = max(merged[-1][2], s[2])
        else:
            merged.append(s)
    ps = pauses(words)
    out = []
    for s, e, peak in merged:
        s = max(0.0, s - PAD)
        e = min(duration, e + PAD)
        # move outward to a pause
        before = [p for p in ps if p[1] <= s and s - p[1] <= 30]
        if before:
            s = (before[-1][0] + before[-1][1]) / 2
        after = [p for p in ps if p[0] >= e and p[0] - e <= 30]
        if after:
            e = (after[0][0] + after[0][1]) / 2
        if s < 60:          # don't leave a stub of intro before a cut
            s = 0.0
        if duration - e < 60:
            e = duration
        mask = (grid >= s) & (grid <= e)
        out.append({"start": round(s, 2), "end": round(e, 2), "peak_score": round(peak, 1),
                    "mean_score": round(float(sc[mask].mean()) if mask.any() else 0, 2),
                    "weak": bool(e - s < MIN_SEG + 2 * PAD or peak < HI * 1.5)})
    # merge after padding
    fin = []
    for s in out:
        if fin and s["start"] <= fin[-1]["end"]:
            fin[-1]["end"] = max(fin[-1]["end"], s["end"])
            fin[-1]["peak_score"] = max(fin[-1]["peak_score"], s["peak_score"])
            fin[-1]["weak"] = fin[-1]["weak"] and s["weak"]
        else:
            fin.append(s)
    return fin, (grid, sc, wts)


def text_between(words, a, b, max_words=None, tail=False):
    ws = [w[2].strip() for w in words if a <= w[0] < b]
    if max_words:
        ws = ws[-max_words:] if tail else ws[:max_words]
    return " ".join(ws)


def kept_intervals(duration, cuts):
    keep, t = [], 0.0
    for c in cuts:
        if c["start"] > t:
            keep.append([round(t, 3), c["start"]])
        t = max(t, c["end"])
    if duration - t > 0.5:
        keep.append([round(t, 3), round(duration, 3)])
    return keep


def map_words(words, keep):
    """Map raw-time words into the cut timeline (drops words in cuts)."""
    out, off = [], 0.0
    for a, b in keep:
        for w in words:
            if a <= w[0] and w[1] <= b:
                out.append([w[0] - a + off, w[1] - a + off, w[2]])
        off += b - a
    return out


def main():
    from audio import load, save, SR
    data = os.environ.get("PAD_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
    manifest = json.load(open(os.path.join(data, "hosts", "manifest.json")))
    only = set(sys.argv[1:])
    log_lines = ["# V4V CTA cut log", "",
                 f"Params: WIN={WIN}s STEP={STEP}s HI={HI} LO={LO} MERGE_GAP={MERGE_GAP}s PAD={PAD}s", ""]
    summary = []
    for h in manifest:
        if only and h["id"] not in only:
            continue
        tpath = os.path.join(data, "transcripts", "hosts", h["id"], "transcript.json")
        if not os.path.exists(tpath):
            print("no transcript yet:", h["id"]); continue
        T = json.load(open(tpath))
        words, duration = [tuple(w) for w in T["words"]], T["duration"]
        cuts, (grid, sc, wts) = find_segments(words, duration)
        keep = kept_intervals(duration, cuts)
        hdir = os.path.join(data, "hosts_cut", h["id"])
        ndir = os.path.join(data, "v4v_cta", h["id"])
        os.makedirs(hdir, exist_ok=True)
        os.makedirs(ndir, exist_ok=True)
        x = load(h["path"])
        fade = int(0.02 * SR)
        parts = []
        for a, b in keep:
            seg = x[int(a * SR):int(b * SR)].copy()
            if len(seg) > 2 * fade:
                seg[:fade] *= np.linspace(0, 1, fade)
                seg[-fade:] *= np.linspace(1, 0, fade)
            parts.append(seg)
        y = np.concatenate(parts) if parts else x[:0]
        save(os.path.join(hdir, "audio.wav"), y)
        cw = map_words(words, keep)
        json.dump({"words": cw, "duration": len(y) / SR, "keep": keep, "source": h["path"]},
                  open(os.path.join(hdir, "words.json"), "w"))
        for k, c in enumerate(cuts):
            c["first_line"] = text_between(words, c["start"], c["end"], 30)
            c["last_line"] = text_between(words, c["start"], c["end"], 30, tail=True)
            c["duration"] = round(c["end"] - c["start"], 2)
            from collections import Counter
            cnt = Counter()
            for w, wt in zip(words, wts):
                if c["start"] <= w[0] < c["end"] and wt > 0:
                    cnt[norm(w[2])] += wt
            c["top_cues"] = [k for k, _ in cnt.most_common(10)]
            seg_audio = os.path.join(ndir, f"cta_{k}.mp3")
            save(seg_audio, x[int(c["start"] * SR):int(c["end"] * SR)])
            c["audio"] = seg_audio
            json.dump({"host": h["id"], "start": c["start"], "end": c["end"],
                       "words": [[w[0] - c["start"], w[1] - c["start"], w[2]] for w in words
                                 if c["start"] <= w[0] < c["end"]]},
                      open(os.path.join(ndir, f"cta_{k}.json"), "w"))
        np.save(os.path.join(ndir, "scores.npy"), np.stack([grid, sc]))
        plan = {"host": h["id"], "title": h["title"], "duration": duration,
                "cut_total": round(sum(c["duration"] for c in cuts), 1),
                "cut_duration": round(len(y) / SR, 2), "cuts": cuts, "keep": keep}
        json.dump(plan, open(os.path.join(hdir, "cut_plan.json"), "w"), indent=1)
        summary.append({k: plan[k] for k in ("host", "duration", "cut_total")} | {"n_cuts": len(cuts)})
        log_lines.append(f"## {h['id']} — {h['title']} ({duration/60:.1f} min)")
        log_lines.append(f"Cut {len(cuts)} segment(s), {plan['cut_total']/60:.1f} min total.")
        for c in cuts:
            log_lines.append(f"- {c['start']/60:.2f}–{c['end']/60:.2f} min ({c['duration']/60:.1f} min) "
                             f"peak {c['peak_score']} {'WEAK ' if c['weak'] else ''}")
            log_lines.append(f"  - top cues: {', '.join(c['top_cues'])}")
            log_lines.append(f"  - first: “{c['first_line']}”")
            log_lines.append(f"  - last: “{c['last_line']}”")
        log_lines.append("")
        print(h["id"], f"{duration/60:.0f}m", [(round(c['start']/60, 1), round(c['end']/60, 1), c['peak_score']) for c in cuts], flush=True)
    path = os.path.join(data, "v4v_cta", "CUT_LOG.md")
    if not only:
        open(path, "w").write("\n".join(log_lines))
        json.dump(summary, open(os.path.join(data, "v4v_cta", "summary.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
