#!/usr/bin/env python3
"""Dynamic-slot alignment (ported from the offline prototype).

Aligns two transcripts of the SAME episode downloaded at different times.
Dynamic ads differ per download; the rest is identical. Alignment is on
word sequences (monotone anchor chain + difflib on gaps), never on
timestamps, because the two downloads have different total durations
and ad slots shift everything after them.

Method (frozen v5b from the prototype, 2026-11-09):
  1. Normalise words (lowercase, strip punctuation).
  2. Anchor on 5-word shingles that occur exactly once in each
     transcript. (The prototype file's docstring said 8-word shingles;
     the code uses 5. This port says 5.)
  3. Take the longest increasing subsequence of anchors (monotone chain).
  4. Between anchors, diff the small gaps with difflib.
  5. Differing regions feed a density filter: a 40-token sliding window
     with >= 0.6 differing-token density marks coarse stretches; starts
     are refined by density extension (10-token window >= 0.5, cap 60
     tokens), ends by token-diff extension (stop at 3+ consecutive
     matching tokens, cap 60 tokens). Minimum slot 8s.
  6. Slots are reported on the A clock, with the corresponding B span
     mapped through the anchor chain / differing regions.

Ported 2026-11-11 for the dynamic-slot integration (DESIGN.md). The
prototype file (dynamic_slots/align.py) stays as the frozen reference;
its TRUTH scoring block and the v1 merge-based regions_to_slots path
were dropped here — only density_slots() output is persisted.

Usage:
  python3 slot_align.py <A_dir> <B_dir>
  A_dir/B_dir hold chunk_*.json files (pipeline format, absolute times).
"""
import difflib
import glob
import json
import os
import re
from bisect import bisect_left

ALIGNER_VERSION = "v5b"
SHINGLE = 5
DENSITY_WINDOW = 40
DENSITY_THRESH = 0.6
MIN_SLOT_S = 8.0


def load_words(d):
    words = []
    for f in sorted(glob.glob(os.path.join(d, "chunk_*.json"))):
        words.extend(json.load(open(f))["words"])
    # words: [start, end, text] with absolute times; sort defensively
    words.sort(key=lambda w: w[0])
    return words


def norm(tok):
    return re.sub(r"[^a-z0-9]", "", tok.lower())


def norm_tokens(words):
    """Return (tokens, kept_idx) where tokens are normalised non-empty words
    and kept_idx maps token position -> index into words."""
    toks, kept = [], []
    for i, w in enumerate(words):
        t = norm(w[2])
        if t:
            toks.append(t)
            kept.append(i)
    return toks, kept


def anchors(ta, tb):
    """Unique-in-both 5-word shingles -> monotone anchor chain.
    Returns (chain, n_candidates) where chain is [(a_pos, b_pos), ...]."""
    def shingle_pos(toks):
        pos = {}
        for i in range(len(toks) - SHINGLE + 1):
            key = tuple(toks[i:i + SHINGLE])
            pos.setdefault(key, []).append(i)
        return pos
    pa, pb = shingle_pos(ta), shingle_pos(tb)
    pts = []
    for key, la in pa.items():
        lb = pb.get(key)
        if la and lb and len(la) == 1 and len(lb) == 1:
            pts.append((la[0], lb[0]))
    pts.sort()
    # Longest increasing subsequence on b (strictly increasing).
    # Patience algorithm with parent pointers.
    tails, tails_idx, parent = [], [], {}
    for idx, (a, b) in enumerate(pts):
        j = bisect_left(tails, b)
        parent[idx] = tails_idx[j - 1] if j > 0 else None
        if j == len(tails):
            tails.append(b)
            tails_idx.append(idx)
        else:
            tails[j] = b
            tails_idx[j] = idx
    chain = []
    cur = tails_idx[-1] if tails_idx else None
    while cur is not None:
        chain.append(pts[cur])
        cur = parent[cur]
    chain.reverse()
    return chain, len(pts)


def diff_regions(ta, tb, chain):
    """Return list of differing regions as (a0, a1, b0, b1) token spans,
    where [a0, a1) in A and [b0, b1) in B differ. Equal anchors bracket gaps."""
    segs = []
    prev_a, prev_b = 0, 0
    for (a, b) in chain:
        segs.append(("gap", prev_a, a, prev_b, b))
        segs.append(("eq", a, a + SHINGLE, b, b + SHINGLE))
        prev_a, prev_b = a + SHINGLE, b + SHINGLE
    segs.append(("gap", prev_a, len(ta), prev_b, len(tb)))
    regions = []
    for kind, a0, a1, b0, b1 in segs:
        if kind == "eq":
            continue
        if a0 == a1 and b0 == b1:
            continue
        sm = difflib.SequenceMatcher(a=ta[a0:a1], b=tb[b0:b1], autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                regions.append((a0 + i1, a0 + i2, b0 + j1, b0 + j2))
    regions.sort()
    return regions


def density_slots(regions, wa, ka, window=DENSITY_WINDOW, thresh=DENSITY_THRESH,
                  min_dur=MIN_SLOT_S):
    """Slots from per-word diff density: a token differs if inside any
    differing region. Slide a window over A tokens; a slot is a maximal
    stretch where the fraction of differing tokens exceeds thresh.
    Boundaries are refined to the first/last differing token inside.

    Returns (slots, total_diff, n_tokens) where each slot is
    [start_s, end_s, diff_words, span_tokens, a_tok_start, a_tok_end].
    The token range is included so callers can map the slot to the B
    side; the prototype's version returned only the first four fields.
    Boundary refinement (v5b, hybrid): STARTS use density extension —
    ad openings begin with common words ("hello", "this episode") that
    can match the other download by chance, so starts need the density
    bridge; ENDS use token-diff extension — the first run of 3+
    matching tokens marks real content resumption, and density overshot
    ends by ~2-3s. Cap 60 tokens (~20s) each side.
    """
    n = len(ka)
    differs = [False] * n
    for (a0, a1, b0, b1) in regions:
        for i in range(a0, a1):
            differs[i] = True
    total_diff = sum(differs)
    # sliding window density (window tokens, centred)
    half = window // 2
    slots = []
    i = 0
    # prefix sums for fast density
    pref = [0] * (n + 1)
    for j in range(n):
        pref[j + 1] = pref[j] + (1 if differs[j] else 0)

    def dens(c):
        lo, hi = max(0, c - half), min(n, c + half)
        return (pref[hi] - pref[lo]) / max(1, hi - lo)
    in_slot = False
    start = 0
    for c in range(n):
        d = dens(c)
        if d >= thresh and not in_slot:
            in_slot, start = True, c
        elif d < thresh and in_slot:
            in_slot = False
            slots.append((start, c))
    if in_slot:
        slots.append((start, n))
    half2 = 5

    def dens2(c):
        lo, hi = max(0, c - half2), min(n, c + half2)
        return (pref[hi] - pref[lo]) / max(1, hi - lo)

    def is_dense(c):
        return 0 <= c < n and dens2(c) >= 0.5
    out = []
    for (s0, s1) in slots:
        ds = [i for i in range(s0, s1) if differs[i]]
        if not ds:
            continue
        a, gap = ds[0], 0
        while a > 0 and ds[0] - a < 60:
            if is_dense(a - 1):
                a -= 1
                gap = 0
            else:
                gap += 1
                if gap > 3:
                    break
                a -= 1
        b, gap = ds[-1], 0
        while b < n - 1 and b - ds[-1] < 60:
            if differs[b + 1]:
                b += 1
                gap = 0
            else:
                gap += 1
                if gap > 2:
                    break
                b += 1
        while b > ds[-1] and not differs[b]:
            b -= 1
        # NOTE: no start trim — the density extension deliberately reaches
        # back over coincidental matching common words at the ad opening;
        # trimming to the first differing token undoes it (v5 bug, fixed).
        t0 = wa[ka[a]][0]
        t1 = wa[ka[b]][1]
        if t1 - t0 >= min_dur:
            out.append([round(t0, 1), round(t1, 1), len(ds), s1 - s0,
                        a, b + 1])
    return out, total_diff, n


def _map_a_to_b(chain, a_idx, n_b):
    """Approximate B token position for an A token position, by linear
    interpolation between the surrounding anchors. Used only to bound
    the B-side span of a slot (the gate checks B words in that span
    with a time margin), never for edge placement."""
    if not chain:
        return max(0, min(n_b, a_idx))
    a_positions = [a for a, _ in chain]
    j = bisect_left(a_positions, a_idx)
    if j == 0:
        a0, b0 = chain[0]
        return max(0, b0 - (a0 - a_idx))
    if j >= len(chain):
        a0, b0 = chain[-1]
        return min(n_b, b0 + (a_idx - a0))
    a0, b0 = chain[j - 1]
    a1, b1 = chain[j]
    if a1 == a0:
        return b0
    return int(round(b0 + (b1 - b0) * (a_idx - a0) / (a1 - a0)))


def align_words(a_words, b_words):
    """Align two word lists (pipeline format: [start, end, text]) and
    return the v5b density slots on the A clock plus diagnostics.

    Returns dict with keys: aligner_version, slots, differing_token_rate,
    n_anchors, n_anchor_candidates, a_duration, b_duration. Each slot is
    {"start", "end", "diff_words", "a_words", "b_words", "density",
    "b_start", "b_end"} — b_start/b_end are the corresponding B-clock
    times (for the B-side gate check only; slot edges are A-clock)."""
    ta, ka = norm_tokens(a_words)
    tb, kb = norm_tokens(b_words)
    chain, n_cand = anchors(ta, tb)
    regions = diff_regions(ta, tb, chain)
    ds, total_diff, n_tok = density_slots(regions, a_words, ka)
    a_dur = a_words[-1][1] if a_words else 0.0
    b_dur = b_words[-1][1] if b_words else 0.0
    slots = []
    for (t0, t1, diff_words, span_tokens, a0, a1) in ds:
        # B token range: union of differing regions overlapping the slot's
        # A range, widened by anchor interpolation as a fallback.
        b_lo, b_hi = None, None
        for (ra0, ra1, rb0, rb1) in regions:
            if ra0 < a1 and ra1 > a0:
                b_lo = rb0 if b_lo is None else min(b_lo, rb0)
                b_hi = rb1 if b_hi is None else max(b_hi, rb1)
        if b_lo is None:
            b_lo = _map_a_to_b(chain, a0, len(tb))
            b_hi = _map_a_to_b(chain, a1, len(tb))
        b_lo = max(0, min(len(tb), b_lo))
        b_hi = max(b_lo, min(len(tb), b_hi))
        if b_lo < b_hi and b_words:
            b_start = b_words[kb[b_lo]][0]
            b_end = b_words[kb[b_hi - 1]][1]
        else:
            b_start = b_end = 0.0
        a_words_n = max(1, a1 - a0)
        slots.append({
            "start": t0, "end": t1,
            "diff_words": diff_words,
            "a_words": a_words_n,
            "b_words": max(0, b_hi - b_lo),
            "density": round(diff_words / a_words_n, 3),
            "b_start": round(b_start, 1), "b_end": round(b_end, 1),
        })
    return {
        "aligner_version": ALIGNER_VERSION,
        "slots": slots,
        "differing_token_rate": round(total_diff / max(1, n_tok), 4),
        "n_anchors": len(chain),
        "n_anchor_candidates": n_cand,
        "a_duration": round(a_dur, 1),
        "b_duration": round(b_dur, 1),
    }


def align_dirs(a_dir, b_dir):
    """Load chunk_*.json from two directories and align them."""
    return align_words(load_words(a_dir), load_words(b_dir))


def main():
    import sys
    a_dir, b_dir = sys.argv[1], sys.argv[2]
    res = align_dirs(a_dir, b_dir)
    print(f"aligner {res['aligner_version']}: "
          f"{res['n_anchors']} anchors "
          f"({res['n_anchor_candidates']} candidates), "
          f"differing tokens {res['differing_token_rate']:.1%}")
    for s in res["slots"]:
        print(f"  {s['start']:7.1f}–{s['end']:7.1f} "
              f"({s['end'] - s['start']:5.1f}s, diff-words {s['diff_words']}/"
              f"{s['a_words']}, density {s['density']:.2f}, "
              f"B {s['b_start']:.1f}–{s['b_end']:.1f})")


if __name__ == "__main__":
    main()
