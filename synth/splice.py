"""Splice real ad clips into V4V-cut host audio with exact ground truth.

usage: splice.py --n 20 --seed 1234 [--out ../data/spliced/batch1]

Per output episode:
  - host: a V4V-cut host (data/hosts_cut/<id>/audio.wav) or a random
    window of it (long hosts), so output length varies (~35-100 min)
  - 2-6 ad breaks; slot kinds: pre-roll (t=0), post-roll (end), mid-roll.
    Mid-rolls are inserted at a word gap that is either a sentence end /
    pause >= 0.5 s ("pause") or a gap inside a sentence ("mid_sentence").
    Never inside a word. Some breaks are 2-ad pods.
  - ad clips loudness-matched to the host (EBU R128 two-pass linear
    ffmpeg loudnorm, target = host integrated loudness)
  - randomized crossfades 0-300 ms at each edge (equal-power), optional
    short silence gap (0-400 ms) on some edges
Ground truth: ad start = first output sample containing ad audio (start
of the fade-in overlap), ad end = last sample containing ad audio.
Times are exact in the output timeline (sample-accurate, SR=44100).
Writes gt.json in the eval harness "generic" format
({episode: {"ads": [[start, end, label], ...]}}), gt_ads.json (one entry
per clip, pods unmerged), and per-episode meta.json.
"""
import argparse, json, os, random, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio import load, save, loudness, loudnorm_to, SR

DATA = os.environ.get("PAD_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
MIN_SPACING = 240.0     # s between mid-roll breaks
EDGE_MARGIN = 120.0     # no mid-roll this close to start/end


def brand_key(b):
    """'rosettastone.com' == 'Rosetta Stone' == 'RosettaStone'."""
    import re
    return re.sub(r"(com|org|net|io)$", "", re.sub(r"[^a-z0-9]", "", b.lower()))


def word_gaps(words):
    """Candidate insertion gaps: (t_insert, kind)."""
    out = []
    for a, b in zip(words, words[1:]):
        gap = b[0] - a[1]
        if gap < 0:
            continue
        txt = a[2].strip()
        t = a[1] + gap / 2
        if gap >= 0.5 or txt.endswith((".", "?", "!")):
            out.append((t, "pause"))
        elif gap < 0.25 and not txt.endswith((",", ".", "?", "!", ";", ":")):
            out.append((t, "mid_sentence"))
    return out


def equal_power(n):
    x = np.linspace(0, 1, n, endpoint=False) + 0.5 / max(n, 1)
    return np.sin(x * np.pi / 2), np.cos(x * np.pi / 2)


def build_episode(rng, host, host_audio, host_words, host_lufs, clips, ad_cache, eid, out_dir):
    dur = len(host_audio) / SR
    n_breaks = rng.randint(2, 6)
    slots = []
    if rng.random() < 0.5:
        slots.append(("pre", 0.0))
    if rng.random() < 0.3:
        slots.append(("post", dur))
    gaps = [g for g in word_gaps(host_words) if EDGE_MARGIN < g[0] < dur - EDGE_MARGIN]
    want_mid = max(0, n_breaks - len(slots))
    tries = 0
    mids = []
    while len(mids) < want_mid and tries < 2000 and gaps:
        tries += 1
        kind = "pause" if rng.random() < 0.6 else "mid_sentence"
        pool = [g for g in gaps if g[1] == kind] or gaps
        t, k = rng.choice(pool)
        if all(abs(t - m[1]) >= MIN_SPACING for m in mids):
            mids.append((k, t))
    slots += mids
    slots.sort(key=lambda s: s[1])
    # assign clips (no brand repeated within an episode)
    used_brands, breaks = set(), []
    for kind, t in slots:
        n_ads = 2 if rng.random() < 0.25 else 1
        chosen = []
        for _ in range(n_ads):
            pool = clips
            if isinstance(clips, dict):  # {"pools": [...], "mix": p}
                pool = clips["pools"][1] if rng.random() < clips["mix"] else clips["pools"][0]
            cands = [c for c in pool if brand_key(c["brand"]) not in used_brands]
            c = rng.choice(cands)
            used_brands.add(brand_key(c["brand"]))
            chosen.append(c)
        breaks.append({"slot": kind, "host_t": t, "clips": chosen})
    # assemble
    out, gt_ads, cursor_host = [], [], 0
    out_len = 0
    detail = []

    def append(x):
        nonlocal out_len
        out.append(x)
        out_len += len(x)

    for br in breaks:
        cut = int(round(br["host_t"] * SR))
        host_seg = host_audio[cursor_host:cut].copy()
        cursor_host = cut
        for ci, c in enumerate(br["clips"]):
            ad = ad_cache(c).copy()
            xf_in = rng.randint(0, 300) if not (br["slot"] == "pre" and ci == 0 and len(host_seg) == 0) else 0
            xf_out = rng.randint(0, 300)
            gap_in = rng.randint(50, 400) if rng.random() < 0.25 else 0
            if br["slot"] == "pre" and ci == 0:
                gap_in = 0
            n_in = int(xf_in * SR / 1000)
            # crossfade host tail with ad head (only when previous material exists)
            if ci == 0:
                prev = host_seg
            else:
                prev = np.zeros(0, dtype=np.float32)
            if gap_in:
                n_in = 0
            if n_in and len(prev) >= n_in and len(ad) > 2 * n_in:
                fi, fo = equal_power(n_in)
                tail = prev[-n_in:] * fo + ad[:n_in] * fi
                append(prev[:-n_in])
                ad_start = out_len
                append(tail.astype(np.float32))
                append(ad[n_in:])
            else:
                append(prev)
                if gap_in:
                    append(np.zeros(int(gap_in * SR / 1000), dtype=np.float32))
                ad_start = out_len
                append(ad)
                n_in = 0
            ad_end = out_len
            gt_ads.append({"start": ad_start / SR, "end": ad_end / SR, "clip": c["id"],
                           "brand": c["brand"], "source": c["source"], "type": c["type"],
                           "genre": c.get("genre", "tech"), "source_show": c.get("source_show", c["source"]),
                           "slot": br["slot"], "insert_kind": br["slot"] if br["slot"] != "mid" else None,
                           "pod_index": ci, "pod_size": len(br["clips"]),
                           "xfade_in_ms": round(n_in * 1000 / SR), "gap_in_ms": gap_in,
                           "xf_out_req_ms": xf_out})
        # fade-out: overlap ad tail with following host head
        last = gt_ads[-1]
        n_out = int(last["xf_out_req_ms"] * SR / 1000)
        gap_out = rng.randint(50, 400) if rng.random() < 0.25 else 0
        if br["slot"] == "post":
            n_out, gap_out = 0, 0
        nxt_cut = int(round(breaks[breaks.index(br) + 1]["host_t"] * SR)) if br is not breaks[-1] else len(host_audio)
        head_avail = nxt_cut - cursor_host
        if gap_out:
            n_out = 0
            append(np.zeros(int(gap_out * SR / 1000), dtype=np.float32))
        elif n_out and head_avail > 2 * n_out:
            # rewrite the last n_out samples of output (ad tail) mixed with host head
            buf = np.concatenate(out)
            out.clear()
            fi, fo = equal_power(n_out)
            buf[-n_out:] = buf[-n_out:] * fo + host_audio[cursor_host:cursor_host + n_out] * fi
            out.append(buf)
            cursor_host += n_out
        else:
            n_out = 0
        last["xfade_out_ms"] = round(n_out * 1000 / SR)
        last["gap_out_ms"] = gap_out
    append(host_audio[cursor_host:].copy())
    y = np.concatenate(out)
    peak = float(np.abs(y).max())
    if peak > 0.999:
        y = y * (0.999 / peak)
    for a in gt_ads:
        a["start"] = round(a["start"], 4)
        a["end"] = round(a["end"], 4)
        a["slot"] = a["slot"]
        a.pop("insert_kind", None)
        a.pop("xf_out_req_ms", None)
    for a, br in zip([g for g in gt_ads if g["pod_index"] == 0], breaks):
        a["host_insert_t"] = round(br["host_t"], 3)
    os.makedirs(out_dir, exist_ok=True)
    save(os.path.join(out_dir, "audio.mp3"), y)
    # merged GT spans (pods -> one span)
    spans = []
    for a in gt_ads:
        if spans and a["start"] - spans[-1][1] < 0.5:
            spans[-1][1] = a["end"]
            spans[-1][2] += " + " + a["brand"]
        else:
            spans.append([a["start"], a["end"], a["brand"]])
    meta = {"episode": eid, "host": host["id"], "host_title": host["title"],
            "host_window": host.get("_window"), "duration": round(len(y) / SR, 3),
            "host_lufs": host_lufs, "ads": gt_ads, "gt_spans": spans,
            "description": host["description"],
            "sponsors_oracle": sorted({a["brand"] for a in gt_ads})}
    json.dump(meta, open(os.path.join(out_dir, "meta.json"), "w"), indent=1)
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default=os.path.join(DATA, "spliced", "batch1"))
    ap.add_argument("--min-win", type=float, default=35 * 60)
    ap.add_argument("--max-win", type=float, default=100 * 60)
    ap.add_argument("--dai-mix", type=float, default=0.0,
                    help="probability an ad is drawn from data/ads/dai_clips.json (batch2: 0.6)")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    manifest = {h["id"]: h for h in json.load(open(os.path.join(DATA, "hosts", "manifest.json")))}
    hosts = sorted(h for h in manifest if os.path.exists(os.path.join(DATA, "hosts_cut", h, "words.json")))
    clips = json.load(open(os.path.join(DATA, "ads", "clips.json")))
    qual = os.path.join(DATA, "ads", "clip_quality.json")
    if os.path.exists(qual):
        bad = {k for k, v in json.load(open(qual)).items() if not v.get("ok", True)}
        clips = [c for c in clips if c["id"] not in bad]
    if args.dai_mix > 0:
        dai = [c for c in json.load(open(os.path.join(DATA, "ads", "dai_clips.json"))) if c.get("splice_ok")]
        print(f"pool: {len(clips)} tech chapter clips + {len(dai)} DAI clips, mix {args.dai_mix}")
        clips = {"pools": [clips, dai], "mix": args.dai_mix}
    norm_dir = os.path.join(DATA, "ads", "norm")
    os.makedirs(norm_dir, exist_ok=True)
    # host order: each host once, then longest hosts again (different window)
    by_len = sorted(hosts, key=lambda h: -json.load(open(os.path.join(DATA, "hosts_cut", h, "words.json")))["duration"])
    order = (hosts + by_len)[:args.n]
    gt, gt_ads_all, metas = {}, {}, []
    lufs_cache = {}
    for i, hid in enumerate(order):
        W = json.load(open(os.path.join(DATA, "hosts_cut", hid, "words.json")))
        wav = os.path.join(DATA, "hosts_cut", hid, "audio.wav")
        full_dur = W["duration"]
        win_len = rng.uniform(args.min_win, args.max_win)
        if full_dur > win_len + 60:
            w0 = rng.uniform(0, full_dur - win_len)
        else:
            w0, win_len = 0.0, full_dur
        host_audio = load(wav, start=w0, dur=win_len)
        words = [[w[0] - w0, w[1] - w0, w[2]] for w in W["words"] if w0 <= w[0] and w[1] <= w0 + win_len]
        tmp = os.path.join(norm_dir, f"_host_{hid}_{i}.wav")
        save(tmp, host_audio)
        host_lufs = float(loudness(tmp)["input_i"])
        os.remove(tmp)
        h = dict(manifest[hid])
        h["_window"] = [round(w0, 3), round(w0 + win_len, 3)]

        def ad_cache(c, target=host_lufs):
            key = f"{c['id']}@{target:.1f}"
            p = os.path.join(norm_dir, key + ".wav")
            if not os.path.exists(p):
                lufs_cache[key] = loudnorm_to(c["path"], p, target)
            return load(p)

        eid = f"synth-{i:03d}-{hid}"
        meta = build_episode(rng, h, host_audio, words, host_lufs, clips, ad_cache,
                             eid, os.path.join(args.out, eid))
        gt[eid] = {"ads": meta["gt_spans"]}
        gt_ads_all[eid] = meta["ads"]
        metas.append(meta)
        print(eid, f"{meta['duration']/60:.1f}m", [(round(a['start']), round(a['end']), a['slot']) for a in meta["ads"]], flush=True)
    json.dump(gt, open(os.path.join(args.out, "gt.json"), "w"), indent=1)
    json.dump(gt_ads_all, open(os.path.join(args.out, "gt_ads.json"), "w"), indent=1)
    json.dump({"seed": args.seed, "n": args.n, "hosts": order, "dai_mix": args.dai_mix,
               "min_win": args.min_win, "max_win": args.max_win}, open(os.path.join(args.out, "batch.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
