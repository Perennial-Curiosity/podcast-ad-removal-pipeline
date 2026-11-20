#!/usr/bin/env python3
"""Back-to-back ad split: rescue the head/tail of a fused heat-map
region that belongs to a different sponsor than the block's label.

DIAGNOSIS (2026-11-13, see NOTE.md): two zero-gap ad pairs lose
their first ad the same way. One heat-map region spans both ads
(TTS Ridge+AG1: 1731-1863s; synthetic Stop Box+SuperPower:
5920-6075s) because votes never drop below 2 across the boundary.
The label picker names the whole region for ONE sponsor (TTS: AG1
wins over RIDGE alphabetically — both have in-region mentions;
synthetic: only SuperPower has any method segment in the region),
and the mention-span cap + edge refinement, both anchored on the
picked sponsor's mentions, trim the block to that sponsor alone —
erasing the other ad entirely (Ridge coverage 0.0, Stop Box 0.0).

MECHANISM (post-processing on the final blocks + the raw method
candidates — no pipeline change): for each final block, rebuild the
raw heat-map region it came from. If the region extends >=30s past
the block on either side (the head before its start, the tail after
its end) and that extension holds >=2 mentions of a DIFFERENT
description sponsor whose mention span is cleanly ordered against
the block sponsor's mentions (all of the other sponsor's mentions
end before the block sponsor's first mention in the region, or
start after its last), the extension is a second ad the pipeline
trimmed away — emit it as a rescue block. Mentions are counted in
a window extended 30s outward from the region (the region starts
where the SECOND vote arrives, so the first ad's opening mention
can sit just before it), but >=1 mention must fall strictly inside
the extension. The rescue block runs
from the other sponsor's best segment boundary (cluster start/end
preferred — mention-anchored and pause-snapped; else the earliest
cta/brand/duplicate/chapter segment start/end; else the region
edge), bounded to the other sponsor's mention span +-30s (the
pipeline's own cap bound), and ends/starts exactly at the block
edge — which refinement already placed at the evidence switch
(TTS: AG1 cluster start 1795.8, 0.5s from the true boundary;
synthetic: SuperPower cta start 5968.7, 1.1s from it).

Guards: >=2 mentions, mention span >=5s, strict ordering (no
interleaving), the other sponsor must not already have a block
overlapping the extension, the rescue must be >=30s and <=720s,
and it is clipped against neighbouring blocks. A single stray
mention (e.g. one 'Perfect Jean' mention inside the Ridge window)
cannot fire it.

URL-ANCHORED FALLBACK (fusion fix, 2026-11-14, A/B test — NOT merged):
when the mention gate finds no candidate, try a cta+url pair for one
stem inside the extension. Two independent methods agreeing on one
sponsor is the same 2-vote evidence as the heat map, and it covers
sponsors Whisper spells differently from the variants (Rorra: Whisper
hears "Rora", variants are ['rorra'] — zero findable mentions, but
cta 'rora' 1010.9-1109.7 + url 'rora' 1045.0-1105.7). The stem must
fuzzy-match a description sponsor (>=0.85, the single-cluster anchor
threshold), both segments must overlap the extension by >=30s, and
the block sponsor must have no mentions inside the extension.
Edges: tail -> block end .. url segment end (the URL is the closing
evidence); head -> cta segment start .. block start (the CTA is the
opening evidence).

Usage (harness): rescue_fused_blocks(blocks, candidates, words,
sponsors) -> (new_blocks, new_sources, rescue_log). blocks are the
5-tuples from detect_ad_blocks; candidates are (s, e, label, method)
from candidates_out; sponsors are the description sponsors (+ house
plugs) passed to detect_ad_blocks.
"""
import difflib
import os
import re
import sys

sys.path.insert(0, os.path.expanduser("~/workspace/podcast-ads/pipeline"))
import run_pipeline as rp

HEATMAP_MIN_AD_LEN = 30.0
MAX_AD_LEN = 720.0
MIN_MENTIONS = 2
MIN_MENTION_SPAN = 5.0
# Segments that carry their own padding and span the pair boundary:
# brand-orphan is the mention span +-45s, url is [first-45s, last+10s].
# They must not set a rescue edge.
PADDED_METHODS = ("brand-orphan", "url")
EDGE_METHODS = ("cluster", "cta", "brand", "duplicate", "chapter")


def _part(label):
    # Labels can be None: the slot flow legitimately emits unnamed
    # dynamic slots (DESIGN.md section 4 — "still marked"), and the
    # chapter/history machinery already treats them as unnamed. A None
    # label has no sponsor part, so it matches nothing and the rescue
    # skips the block, like any other artifact label.
    if not label:
        return ""
    return label.split(":")[-1].strip().lower().replace(" ", "")


def _sponsor_for(part, sponsors):
    for sp in sponsors:
        if sp["label"].lower().replace(" ", "") == part:
            return sp
    return None


def _raw_regions(candidates, duration):
    """Raw heat-map regions (2+ votes, contiguous), exactly as
    detect_ad_blocks builds them before gap-merge/cap/refinement."""
    n = int(duration) + 1
    votes = [0] * n
    for s, e, _l, _m in candidates:
        for sec in range(int(s), min(int(e) + 1, n)):
            votes[sec] += 1
    regions = []
    in_b = False
    for sec in range(n):
        if votes[sec] >= 2 and not in_b:
            in_b, bs = True, sec
        elif votes[sec] < 2 and in_b:
            in_b = False
            regions.append((bs, sec))
    if in_b:
        regions.append((bs, n))
    return regions


def _mention_times(nw, words, variants, lo, hi):
    hits = rp.find_mentions(nw, variants)
    return sorted(words[h][0] for h in hits if lo <= words[h][0] <= hi)


# Single shared TLD set (M15): must equal run_pipeline.URL_TLDS and
# transcript_diff.TRIM_TLDS — a missing TLD is a silent miss.
_TLDS = {"com", "net", "org", "io", "tv", "co", "ai", "me", "tools",
         "app", "dev", "xyz", "online", "store", "tech", "site",
         "cloud", "cc", "gg", "life", "world", "news", "media",
         "agency", "digital", "solutions", "systems", "network",
         "security", "us"}


def _stem_matches(stem, sponsor):
    """Does a candidate stem (e.g. 'rora' from a spoken URL) name this
    description sponsor? Same threshold as the single-cluster anchor
    check in detect_ad_blocks: exact match or SequenceMatcher >= 0.85
    against the sponsor's label, variants, and non-TLD components."""
    stem = stem.lower().replace(" ", "").split(".")[0]
    allowed = set()
    for v in [sponsor["label"]] + list(sponsor.get("variants") or []):
        norm = v.lower().replace(" ", "")
        allowed.add(norm)
        for comp in re.split(r"[.\s]+", v.lower()):
            if comp and comp not in _TLDS:
                allowed.add(comp)
    if stem in allowed:
        return True
    return any(difflib.SequenceMatcher(None, stem, a).ratio() >= 0.85
               for a in allowed)


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def _segment_edge(cands, part, lo, hi, want_start):
    """Best segment edge for sponsor `part` inside [lo, hi].
    Cluster first (mention-anchored, pause-snapped); else the
    earliest start / latest end among cta/brand/duplicate/chapter.
    Padded methods (brand-orphan, url) never set an edge."""
    for method in ("cluster",):
        edges = [s if want_start else e for s, e, l, m in cands
                 if m == method and _part(l) == part
                 and (lo <= (s if want_start else e) <= hi)]
        if edges:
            return min(edges) if want_start else max(edges)
    edges = [s if want_start else e for s, e, l, m in cands
             if m in EDGE_METHODS and m != "cluster"
             and _part(l) == part
             and (lo <= (s if want_start else e) <= hi)]
    if edges:
        return min(edges) if want_start else max(edges)
    return None


def rescue_fused_blocks(blocks, candidates, words, sponsors, sources=None):
    """Split fused blocks: rescue a second sponsor's ad from the
    head/tail of a heat-map region the pipeline trimmed away.
    Returns (new_blocks, new_sources, rescue_log). `sources`, when
    given, is the per-block source list aligned with `blocks`
    (e.g. the slot flow's sources); otherwise every input block is
    treated as a detector block and rescued blocks are marked
    "rescue"."""
    if not blocks or not candidates or not words:
        base_sources = (list(sources) if sources is not None
                        else ["detector"] * len(blocks))
        return list(blocks), base_sources, []
    duration = words[-1][0] + 1
    regions = _raw_regions(candidates, duration)
    if not regions:
        base_sources = (list(sources) if sources is not None
                        else ["detector"] * len(blocks))
        return list(blocks), base_sources, []
    nw = rp.norm_words(words)
    # Mention times per sponsor, whole episode (find_mentions is
    # memoized by the harness; the per-window slicing is cheap).
    mention_cache = {}

    def mentions(sp, lo, hi):
        key = sp["label"]
        if key not in mention_cache:
            hits = rp.find_mentions(nw, sp.get("variants") or {sp["label"]})
            mention_cache[key] = sorted(words[h][0] for h in hits)
        return [t for t in mention_cache[key] if lo <= t <= hi]

    def region_for(bs, be):
        best, best_ov = None, 0.0
        for rs, re in regions:
            ov = max(0.0, min(be, re) - max(bs, rs))
            if ov > best_ov:
                best, best_ov = (rs, re), ov
        return best

    new_blocks = list(blocks)
    sources = (list(sources) if sources is not None
               else ["detector"] * len(blocks))
    log = []
    # Work from the original blocks only (rescues never rescue
    # each other, and never re-split a rescued block).
    for bi, (bs, be, bl, ss, es) in enumerate(blocks):
        bpart = _part(bl)
        bsp = _sponsor_for(bpart, sponsors)
        if bsp is None:
            continue  # artifact label (dry-island etc.): no switch to verify
        region = region_for(bs, be)
        if region is None:
            continue
        rs, re = region
        b_mentions = mentions(bsp, rs, re)
        if not b_mentions:
            continue  # block sponsor has no mentions in its own region

        def _try_side(is_head):
            if is_head:
                ext_lo, ext_hi = rs, bs
                if bs - rs < HEATMAP_MIN_AD_LEN:
                    return
            else:
                ext_lo, ext_hi = be, re
                if re - be < HEATMAP_MIN_AD_LEN:
                    return
            # Other sponsors with >=2 mentions spanning >=5s in the
            # extension, cleanly ordered against the block sponsor.
            # Mentions are counted in a window extended 30s outward
            # from the region: the region starts where the SECOND
            # vote arrives, so the first ad's opening mention can
            # sit just before it (synthetic Stop Box: first mention
            # 5918.8s, region starts 5920s — counting only inside
            # the head leaves 1 mention and the rescue never fires).
            # But >=1 mention must still fall strictly inside the
            # extension, so the sponsor is present in the region
            # head/tail and not merely in the content before it.
            if is_head:
                win_lo, win_hi = rs - 30.0, bs
            else:
                win_lo, win_hi = be, re + 30.0
            best_a, best_ts = None, None
            for sp in sponsors:
                if sp["label"] == bsp["label"]:
                    continue
                ts = mentions(sp, win_lo, win_hi)
                if len(ts) < MIN_MENTIONS:
                    continue
                ts_in = mentions(sp, ext_lo, ext_hi)
                if len(ts_in) < 1:
                    continue
                if ts[-1] - ts[0] < MIN_MENTION_SPAN:
                    continue
                if is_head and not (ts[-1] < b_mentions[0]):
                    continue
                if not is_head and not (ts[0] > b_mentions[-1]):
                    continue
                if best_ts is None or len(ts) > len(best_ts) or (
                        len(ts) == len(best_ts) and ts[-1] > best_ts[-1]):
                    best_a, best_ts = sp, ts
            url_anchored = False
            _url_seg = _cta_seg = None
            if best_a is None:
                # URL-anchored fallback (see module docstring): a cta
                # segment and a url segment for the same stem, both
                # overlapping the extension by >=30s, the stem naming
                # a description sponsor, and the block sponsor silent
                # inside the extension.
                _b_ext = mentions(bsp, ext_lo, ext_hi)
                if not _b_ext:
                    _best = None  # (url_overlap, cta_overlap, sp, url, cta)
                    for _us, _ue, _ul, _um in candidates:
                        if _um != "url":
                            continue
                        _stem = _part(_ul)
                        if _stem in ("dry-island", "voice-anomaly"):
                            continue
                        _uov = _overlap(_us, _ue, ext_lo, ext_hi)
                        if _uov < HEATMAP_MIN_AD_LEN:
                            continue
                        for _cs, _ce, _cl, _cm in candidates:
                            if _cm != "cta" or _part(_cl) != _stem:
                                continue
                            _cov = _overlap(_cs, _ce, ext_lo, ext_hi)
                            if _cov < HEATMAP_MIN_AD_LEN:
                                continue
                            for _sp in sponsors:
                                if _sp["label"] == bsp["label"]:
                                    continue
                                if not _stem_matches(_stem, _sp):
                                    continue
                                _key = (_uov, _cov)
                                if _best is None or _key > _best[0]:
                                    _best = (_key, _sp, (_us, _ue),
                                            (_cs, _ce))
                    if _best is not None:
                        best_a = _best[1]
                        _url_seg, _cta_seg = _best[2], _best[3]
                        url_anchored = True
                if best_a is None:
                    return
            apart = _part(best_a["label"])
            # The other sponsor must not already have a block
            # overlapping the extension — T2-N4: check the blocks
            # rescued earlier in this same call as well as the
            # originals, or a tail rescue and a head rescue for the
            # same sponsor both fire out of one region.
            for (os_, oe, ol, _ss, _es) in new_blocks:
                if _part(ol) == apart and os_ < ext_hi and oe > ext_lo:
                    return
            if is_head:
                if url_anchored:
                    # CTA is the opening evidence; clip to the region.
                    start = max(_cta_seg[0], rs)
                else:
                    edge = _segment_edge(candidates, apart, ext_lo, ext_hi,
                                         want_start=True)
                    start = edge if edge is not None else rs
                    # The pipeline's own cap bound: never start more
                    # than 30s before the other sponsor's first
                    # mention here.
                    start = max(start, best_ts[0] - 30.0)
                end = bs
                # Clip against a preceding block — T2-N4: including
                # rescues appended earlier in this call, so a head
                # rescue cannot overlap a tail rescue for the same
                # sponsor from the same region.
                for (os_, oe, _ol, _ss, _es) in new_blocks:
                    if oe <= bs and oe > start:
                        start = max(start, oe)
                if end - start < HEATMAP_MIN_AD_LEN:
                    return
                if end - start > MAX_AD_LEN:
                    return
            else:
                if url_anchored:
                    # URL is the closing evidence; clip to the region.
                    end = min(_url_seg[1], re)
                else:
                    edge = _segment_edge(candidates, apart, ext_lo, ext_hi,
                                         want_start=False)
                    end = edge if edge is not None else re
                    end = min(end, best_ts[-1] + 30.0)
                start = be
                # T2-N4: clip against following blocks AND rescues
                # appended earlier in this call (see head clip above).
                for (os_, oe, _ol, _ss, _es) in new_blocks:
                    if os_ >= be and os_ < end:
                        end = min(end, os_)
                if end - start < HEATMAP_MIN_AD_LEN:
                    return
                if end - start > MAX_AD_LEN:
                    return
            methods_prefix = bl.rsplit(":", 1)[0] if (bl and ":" in bl) else ""
            label = (f"{methods_prefix}:{best_a['label']}"
                     if methods_prefix else best_a["label"])
            new_blocks.append((round(start, 2), round(end, 2), label,
                               None, None))
            sources.append("rescue")
            log.append({
                "side": "head" if is_head else "tail",
                "from_block": [round(bs, 1), round(be, 1), bl],
                "rescued_sponsor": best_a["label"],
                "rescue_block": [round(start, 2), round(end, 2), label],
                "mentions_in_extension": [round(t, 1) for t in best_ts]
                if not url_anchored else [],
                "anchor": ("url" if url_anchored else "mentions"),
                "region": [rs, re],
            })

        _try_side(is_head=True)
        _try_side(is_head=False)

    paired = sorted(zip(new_blocks, sources), key=lambda p: p[0][0])
    return [b for b, _ in paired], [s for _, s in paired], log
