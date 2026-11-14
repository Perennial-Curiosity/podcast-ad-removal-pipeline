#!/usr/bin/env python3
"""Publisher-transcript differential (AdChisel idea 6, from MinusPod research).

Diffs the pipeline's Whisper transcript of an episode against the
publisher-supplied transcript named by the feed item's
``podcast:transcript`` tag. That publisher transcript was (for the shows
that carry one) produced before the ad reads were spliced in, so it is
already ad-free: any block of speech in the Whisper transcript that the
publisher's copy omits is a candidate ad.

USAGE RULE (MinusPod's rule, adopted verbatim): a gap NEVER marks an ad
on its own. A gap overlapping a detector block CORROBORATES that block
(confidence +0.10, +0.15 when offset_confirmed, capped at 0.95, never
lowered); a gap with no detector block becomes a HELD marker only
(in AdChisel terms: a suspect, on the suspect reel for owner review,
never a chapter). A fetch failure, parse failure, or unreliable
alignment degrades gracefully: no vote, no error, the episode
processes as if this stage had not run.

This module is deliberately self-contained (stdlib only) and does NOT
touch the heat map: it adds no mentions through ``find_mentions`` (so
it cannot trip the orphan-flip constraint — a phonetic/matched mention
source flipping a sponsor off the brand-orphan path), casts no
heat-map vote (so it cannot change which blocks are marked and cannot
regress the 0.673 SB-set baseline), and changes no block edges.
Corroboration is a post-detection annotation; held gaps are suspects.

Method (mirrors pipeline/slot_align.py, which aligns two transcripts
of the same episode the same way):
  1. Parse the publisher transcript (SRT/VTT timed cues, podcast
     namespace JSON segments, or untimed plain text / HTML).
  2. Normalise both word lists (lowercase, strip punctuation) and
     anchor on 5-word shingles occurring exactly once in each
     transcript; take the longest increasing subsequence as the
     monotone anchor chain; diff the small gaps between anchors with
     difflib. Whisper tokens inside a non-equal opcode are
     Whisper-only.
  3. Whisper-only runs of >= 40 words and >= 10s of audio become a
     gap span; gaps within 3s of each other merge into one.
  4. Below 60% word coverage (matched Whisper tokens / all Whisper
     tokens) the comparison is unreliable — wrong episode, heavily
     re-edited transcript, different language — and produces no spans.
  5. When the publisher transcript carries timestamps, check whether
     its time axis jumps across the gap by roughly the gap's own
     length (the signature of a block physically removed rather than
     summarized differently). A gap with that signature is
     ``offset_confirmed`` and carries the higher boost.
  6. Trim (2026-11-13, Omny probe tests): each gap's edges are
     trimmed to the ad copy inside it — start to the first word of
     the ad-copy speech segment (no pad), end to the last ad word,
     floored to the whole second, then +0.50s. The ad copy is found
     from generic evidence only (description sponsors when the
     description names any, spoken URLs, the word "sponsored") plus
     the gap's own pause structure; a trailing repeat of the
     episode's opening bumper is stripped from the end. See
     _trim_gap. The trim only moves edges: it never creates or drops
     a gap, and a gap alone still never marks an ad.

Transcript-type preference (MinusPod order, best first):
  text/vtt, application/srt, application/x-subrip, application/json,
  text/plain, text/html.

Merge-ready: see INTEGRATION.md in this directory for the exact
plug-in points in pipeline/run_pipeline.py (which function, what
config, what state/report fields). Nothing in this file is imported
by run_pipeline.py yet.

Status strings (stored in report/state ``transcript_diff``):
  ok          — comparison ran; gaps may be empty (an empty gap list
                is a real result: the publisher transcript covers the
                final audio, ads included, or the episode has no ads).
  skipped     — no podcast:transcript tag on the item, or disabled
                by config. No vote, no error.
  error       — fetch or parse failure. No vote, no error surfaced.
  unreliable  — word coverage below the 60% floor. No spans, no vote.
"""

import difflib
import html
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from bisect import bisect_left

# --- Tunables (from the MinusPod design doc; generic, not per-show) ---
MIN_GAP_WORDS = 40          # a gap needs >= 40 Whisper-only words
MIN_GAP_SECONDS = 10.0      # ... and >= 10s of audio
MERGE_GAP_SECONDS = 3.0     # gaps within 3s of each other merge into one
MIN_COVERAGE = 0.60         # below 60% word coverage: unreliable, no spans
SHINGLE = 5                 # anchor shingle length (as in slot_align.py)
CORROBORATE_BOOST = 0.10    # confidence boost for a corroborated block
CORROBORATE_BOOST_CONFIRMED = 0.15   # ... when the gap is offset_confirmed
CONFIDENCE_CAP = 0.95
# A publisher time-axis jump counts as "roughly the gap's own length"
# when it is within max(5s, 25% of the gap) of the gap duration.
OFFSET_TOLERANCE_SECONDS = 5.0
OFFSET_TOLERANCE_FRACTION = 0.25
# Reuse a stored ok result for the same transcript URL when a
# reprocess reuses the saved Whisper transcript (MinusPod rule).
CACHE_MAX_AGE_SECONDS = 24 * 3600

# --- Trim tunables (2026-11-13, Omny probe tests, TESTS.md entries 32/33) ---
# A raw gap's edges over-extend: the publisher transcript also omits
# adjacent non-ad material (show intro/outro, house plugs, bumpers),
# so a gap's first/last word is often not an ad word at all. On the
# 9 Omny ads the raw start was exact on mid-rolls but 5.50-7.80s early
# on openers and 8.28-32.50s early on outros, and the raw end
# over-extended by a mean of +13.7s. The trim finds the ad copy inside
# the gap from generic evidence only — description sponsors (when the
# description names any), spoken URLs, and the word "sponsored" —
# plus the gap's own pause structure. It never uses a hardcoded
# brand, show, or episode.
#   start: trimmed to the first word of the ad-copy speech segment,
#     NO pad. The start probe found no ad audio before the trimmed
#     start on any of the 9 ads, and both candidate pads (-0.25s,
#     -0.50s) made every edge worse by exactly the pad size.
#   end: trimmed to the last ad word, floored to the whole second,
#     then +0.50s. The floor reproduces the clip convention the pad
#     was measured on (the pad compensates the floor shortfall, mean
#     0.46s); on the 9 Omny ads this reproduces the measured pad
#     performance exactly (mean |error| 0.28s, max 0.48s, all 9
#     inside 1s). Never pad the raw end: ad content continues past
#     it on 0 of 9 ads.
TRIM_SILENCE_SECONDS = 1.5   # word-gap splitting a gap into speech segments
END_PAD_SECONDS = 0.50       # end-pad on the TRIMMED end only, never raw
TRIM_FALLBACK_MIN_FRACTION = 0.50  # longest-segment fallback needs >= half the gap's words
TRIM_REPEAT_MIN_WORDS = 4    # trailing-repeat strip: at least 4 words
TRIM_REPEAT_MAX_WORDS = 12   # ... and strips at most 12
TRIM_REPEAT_MIN_RATIO = 0.85 # char-level ratio vs the episode opening
# Generic TLD list for recognising a spoken URL in Whisper output
# (".com" renders as a bare "com" token, "BetterHelp.com" as one
# token ending in a TLD). Same list as run_pipeline.py's
# extract_sponsors / find_url_mentions; a missing TLD is a silent
# miss there, and the same applies here. Caveat: a rare ordinary
# word ending in a TLD ("intercom") counts as URL evidence — a
# stray hit only moves a corroboration edge, never marks an ad.
TRIM_TLDS = {"com", "net", "org", "io", "ai", "tv", "co", "us", "tools",
             "app", "dev", "xyz", "online", "store", "tech", "site",
             "cloud", "me", "cc", "gg", "life", "world", "news", "media",
             "agency", "digital", "solutions", "systems", "network",
             "security"}

# Best type first (MinusPod order).
TYPE_PREFERENCE = [
    "text/vtt",
    "application/srt",
    "application/x-subrip",
    "application/json",
    "text/plain",
    "text/html",
]

TIMED_TYPES = {"text/vtt", "application/srt", "application/x-subrip",
               "application/json"}


# ---------------------------------------------------------------------------
# Feed side: finding the publisher transcript
# ---------------------------------------------------------------------------

def extract_transcript_tags(item_body_xml):
    """All podcast:transcript tags in one RSS <item> body.

    Returns a list of {"url": ..., "type": ...} (type may be "" when the
    tag carries none). Attribute order is not assumed.
    """
    tags = []
    for m in re.finditer(r"<podcast:transcript\b[^>]*/?>", item_body_xml):
        tag = m.group(0)
        url = re.search(r'url="([^"]+)"', tag)
        typ = re.search(r'type="([^"]+)"', tag)
        if url:
            tags.append({"url": url.group(1).strip(),
                         "type": (typ.group(1).strip().lower()
                                  if typ else "")})
    return tags


def best_transcript(tags):
    """Pick the best transcript tag by TYPE_PREFERENCE order.

    Unknown types sort after every known type (kept, not dropped — a
    feed may carry a type this list has not seen yet). Returns None
    when the list is empty.
    """
    if not tags:
        return None

    def rank(t):
        try:
            return TYPE_PREFERENCE.index(t["type"])
        except ValueError:
            return len(TYPE_PREFERENCE)

    return sorted(tags, key=rank)[0]


# ---------------------------------------------------------------------------
# Fetching (plain HTTP GET of a URL from the feed — no key, no account)
# ---------------------------------------------------------------------------

def fetch_transcript(url, timeout=60):
    """Fetch a publisher transcript. Returns bytes.

    Raises on any failure; callers that must degrade gracefully use
    run_transcript_diff() instead of calling this directly.
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": "podcast-ad-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------------------
# Parsing: publisher transcript -> [(start, end, word), ...]
# start/end are None for untimed formats (plain text / HTML).
# ---------------------------------------------------------------------------

def _norm(tok):
    return re.sub(r"[^a-z0-9]", "", tok.lower())


def _words_with_times(text, start, end):
    """Split cue text into words, spreading them linearly over [start, end].

    Word-level times inside a cue are an interpolation, not measured
    truth — good enough to place a gap on the Whisper clock (the gap
    edges always come from Whisper word times) and to measure the
    publisher time-axis jump across a gap.
    """
    toks = [t for t in text.split() if _norm(t)]
    if not toks:
        return []
    n = len(toks)
    span = max(0.0, end - start)
    out = []
    for i, t in enumerate(toks):
        ws = start + span * i / n
        we = start + span * (i + 1) / n
        out.append((ws, we, t))
    return out


def _parse_timecode(s):
    """'HH:MM:SS,mmm' (SRT) or 'HH:MM:SS.mmm' (VTT) -> seconds."""
    m = re.match(r"(?:(\d+):)?(\d+):([\d.,]+)", s.strip())
    if not m:
        return None
    h = int(m.group(1)) if m.group(1) else 0
    return h * 3600 + int(m.group(2)) * 60 + float(m.group(3).replace(",", "."))


def parse_srt_vtt(data):
    """Parse SRT or WebVTT bytes -> [(start, end, word), ...] (timed)."""
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"\r\n?", "\n", text)
    # Drop the WEBVTT header line and NOTE blocks; cues are the
    # "start --> end" lines followed by text.
    words = []
    cue_re = re.compile(
        r"(\d+:\d\d:\d\d[.,]\d{3}|\d\d:\d\d[.,]\d{3})\s*-->\s*"
        r"(\d+:\d\d:\d\d[.,]\d{3}|\d\d:\d\d[.,]\d{3})[^\n]*\n(.*?)(?=\n\n|\Z)",
        re.S)
    for m in cue_re.finditer(text):
        start = _parse_timecode(m.group(1))
        end = _parse_timecode(m.group(2))
        if start is None or end is None or end <= start:
            continue
        body = re.sub(r"<[^>]+>", " ", m.group(3))  # strip cue tags
        body = re.sub(r"\s+", " ", body).strip()
        words.extend(_words_with_times(body, start, end))
    if not words:
        raise ValueError("no timed cues parsed from SRT/VTT transcript")
    return words


def parse_podcast_json(data):
    """Parse podcast-namespace transcript JSON -> [(start, end, word), ...].

    Spec shape: {"version": "1.0.0", "segments": [{"startTime": s,
    "endTime": e, "body": "..."}]}. Segments without times degrade to
    untimed words (start/end None) rather than failing the run.
    """
    doc = json.loads(data.decode("utf-8", errors="replace"))
    segments = doc.get("segments") or doc.get("transcript") or []
    if not isinstance(segments, list):
        raise ValueError("podcast JSON transcript has no segments list")
    words = []
    for seg in segments:
        if isinstance(seg, str):
            body, start, end = seg, None, None
        else:
            body = seg.get("body") or seg.get("text") or ""
            start = seg.get("startTime", seg.get("start"))
            end = seg.get("endTime", seg.get("end"))
            try:
                start = float(start) if start is not None else None
                end = float(end) if end is not None else None
            except (TypeError, ValueError):
                start = end = None
        if start is not None and end is not None and end > start:
            words.extend(_words_with_times(body, start, end))
        else:
            words.extend([(None, None, t) for t in body.split() if _norm(t)])
    if not words:
        raise ValueError("no words parsed from podcast JSON transcript")
    return words


def parse_plain_text(data):
    """Parse text/plain (or text/html, tags stripped) -> untimed words."""
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", " ", text)       # also strips HTML tags
    text = html.unescape(text)
    words = [(None, None, t) for t in text.split() if _norm(t)]
    if not words:
        raise ValueError("no words parsed from plain-text transcript")
    return words


def parse_transcript(data, mime_type):
    """Dispatch on MIME type. Unknown types are tried as plain text."""
    mime = (mime_type or "").lower().split(";")[0].strip()
    if mime in ("application/srt", "application/x-subrip", "text/vtt"):
        return parse_srt_vtt(data)
    if mime == "application/json":
        return parse_podcast_json(data)
    return parse_plain_text(data)


# ---------------------------------------------------------------------------
# Alignment: Whisper words vs publisher words
# ---------------------------------------------------------------------------

def _norm_tokens(words):
    """(tokens, kept_idx): normalised non-empty words; kept_idx maps a
    token position back to its index in `words` (same shape as
    slot_align.norm_tokens)."""
    toks, kept = [], []
    for i, w in enumerate(words):
        t = _norm(w[2])
        if t:
            toks.append(t)
            kept.append(i)
    return toks, kept


def _anchors(ta, tb):
    """Unique-in-both SHINGLE-word shingles -> monotone anchor chain.

    Same construction as slot_align.anchors: longest increasing
    subsequence on the publisher side. Returns (chain, n_candidates)
    with chain = [(a_pos, b_pos), ...] in token positions.
    """
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


def _diff_regions(ta, tb, chain):
    """Differing regions as (a0, a1, b0, b1) token spans, from difflib
    on the small gaps between anchors (as in slot_align.diff_regions).
    A token is Whisper-only iff it lies in a non-equal opcode."""
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


def _whisper_only_runs(regions, n_tokens):
    """Merge the Whisper side of all differing regions into maximal
    runs of Whisper-only token positions. Returns [(a0, a1), ...]
    (half-open token ranges). A 'replace' region contributes its whole
    Whisper side: those words do not appear in the publisher copy, so
    they are Whisper-only by the design doc's definition — which is
    also why a gap alone never marks an ad (a paraphrased passage
    diffs the same way an omitted ad does)."""
    only = [False] * n_tokens
    for (a0, a1, _b0, _b1) in regions:
        for i in range(a0, a1):
            only[i] = True
    runs = []
    i = 0
    while i < n_tokens:
        if not only[i]:
            i += 1
            continue
        j = i
        while j < n_tokens and only[j]:
            j += 1
        runs.append((i, j))
        i = j
    return runs


def _offset_confirmed(gap_a0, gap_a1, whisper_only, ta, tb, chain,
                      pub_words, pub_kept, whisper_words, whisper_kept):
    """Publisher time-axis jump check for one gap.

    Finds the nearest matched Whisper token before and after the gap,
    maps them to publisher tokens via the anchor chain (nearest anchor
    at or before / after), and compares the publisher clock jump
    across the gap with the gap's own Whisper-clock duration. Returns
    True only when the publisher transcript is timed and the jump is
    roughly the gap's length. Any missing piece -> False (a gap
    without the signature still counts, at the default confidence).
    """
    if not pub_words or pub_words[0][0] is None:
        return False
    # Nearest equal (matched) Whisper token positions around the gap.
    before = None
    for i in range(gap_a0 - 1, -1, -1):
        if not whisper_only[i]:
            before = i
            break
    after = None
    for i in range(gap_a1, len(ta)):
        if not whisper_only[i]:
            after = i
            break
    if before is None or after is None:
        return False
    # Map Whisper token positions to publisher token positions through
    # the anchor chain: interpolate between surrounding anchors.
    a_pos = [a for a, _ in chain]
    if not a_pos:
        return False

    def map_a_to_b(a_idx):
        j = bisect_left(a_pos, a_idx)
        if j == 0:
            a0, b0 = chain[0]
            return max(0, b0 - (a0 - a_idx))
        if j >= len(chain):
            a0, b0 = chain[-1]
            return min(len(tb), b0 + (a_idx - a0))
        a0, b0 = chain[j - 1]
        a1, b1 = chain[j]
        if a1 == a0:
            return b0
        return int(round(b0 + (b1 - b0) * (a_idx - a0) / (a1 - a0)))

    b_before = map_a_to_b(before)
    b_after = map_a_to_b(after)
    if b_before >= len(tb) or b_after >= len(tb) or b_after <= b_before:
        return False
    t_before_end = pub_words[pub_kept[b_before]][1]
    t_after_start = pub_words[pub_kept[b_after]][0]
    if t_before_end is None or t_after_start is None:
        return False
    gap_start = whisper_words[whisper_kept[gap_a0]][0]
    gap_end = whisper_words[whisper_kept[gap_a1 - 1]][1]
    gap_dur = gap_end - gap_start
    jump = t_after_start - t_before_end
    tol = max(OFFSET_TOLERANCE_SECONDS,
              OFFSET_TOLERANCE_FRACTION * gap_dur)
    return abs(jump - gap_dur) <= tol


# ---------------------------------------------------------------------------
# Trim: find the ad copy inside a gap (2026-11-13, entries 32/33)
# ---------------------------------------------------------------------------

def _sponsor_evidence_tokens(sponsors):
    """(full_names, words): normalised evidence tokens from description
    sponsor names (the labels extract_sponsors returns, e.g.
    "BetterHelp", "Coca-Cola", "AG1"). full_names: the whole name
    with spaces/hyphens removed ("betterhelp", "cocacola", "ag1");
    words: each name word of 4+ chars ("coca", "cola", "better").
    Whisper splits hyphenated and multi-word names ("Coca-Cola" ->
    "coca" "cola"; "BetterHelp" -> "betterhelp" or "better" "help"),
    so both forms are needed. Returns (set(), set()) when sponsors
    is empty/None — the trim then runs on URL/sponsored evidence
    plus the longest-segment fallback alone. (On the Omny episodes
    the descriptions name no sponsor at all, so the trim there runs
    entirely on the generic evidence; the sponsor input matters for
    feeds whose descriptions DO name sponsors.)"""
    full, words = set(), set()
    for name in (sponsors or []):
        parts = [p for p in re.split(r"[^a-z0-9]+", str(name).lower())
                 if p]
        if not parts:
            continue
        joined = "".join(parts)
        if len(joined) >= 3:
            full.add(joined)
        for p in parts:
            if len(p) >= 4:
                words.add(p)
    return full, words


def _is_url_token(tok, raw=""):
    """Is this normalised Whisper token URL evidence? Two renderings:
    a bare TLD token (".com" spoken as "dot com" -> "com"), or a
    single token ending in a TLD ("betterhelpcom" from
    "BetterHelp.com"). The single-token form MUST contain a dot in
    the raw word: without that, ordinary words ending in a TLD
    ("delicious", "previous", "Mexico") false-positive — found on
    the Omny openers and the PC20 simulated drop during
    verification."""
    if tok in TRIM_TLDS:
        return True
    if "." in raw:
        for tld in TRIM_TLDS:
            if len(tok) > len(tld) + 2 and tok.endswith(tld):
                return True
    return False


def _word_evidence(raw_word, tok, full_names, sponsor_words):
    """Evidence points for one Whisper token (raw_word carries the
    original spelling for the URL dot check; tok is normalised): 3
    for a full sponsor name, 2 for a URL token or the word
    "sponsored"/"sponsor", 1 for a single sponsor-name word. 0 for
    everything else. The weights keep a stray common word ("better"
    in ordinary speech, 1 point) from outscoring a real ad segment
    (a URL plus "sponsored" is 4)."""
    if tok in full_names:
        return 3
    if _is_url_token(tok, raw_word):
        return 2
    if tok in ("sponsored", "sponsor"):
        return 2
    if tok in sponsor_words:
        return 1
    return 0


def _split_segments(gap_words):
    """Split a gap's [(start, end, word), ...] into speech segments at
    word-gaps >= TRIM_SILENCE_SECONDS (gap = next start - prev end).
    On the 9 Omny ads the intra-copy gaps reach 1.12s while the
    boundaries that must split are 2.10s and up (opener tag->copy,
    outro sign-off->copy, the 37.98s post-midroll silence), so 1.5s
    sits between them with margin on both sides."""
    segs, cur = [], []
    prev_end = None
    for w in gap_words:
        s, e, _t = w
        if cur and prev_end is not None and s is not None and \
                s - prev_end >= TRIM_SILENCE_SECONDS:
            segs.append(cur)
            cur = []
        cur.append(w)
        if e is not None:
            prev_end = e
    if cur:
        segs.append(cur)
    return segs


def _strip_trailing_repeat(seg_words, head_words, last_evidence_idx):
    """Strip a trailing repeat of the episode's opening bumper from a
    segment's tail. Shows commonly open AND close with the same bumper
    ("This is an iHeart Podcast. Guaranteed Human."); the publisher
    transcript omits both, so the closing bumper lands inside the gap
    as a tail past the last ad word, 0.5s after it — too tight for the
    silence split (intra-ad pauses reach 1.1s). Compares the segment's
    last L words against the episode's first L words at character
    level (tokenisation differs between occurrences: "iHeart" vs
    "i heart" vs "eye heart" join to near-identical strings).
    Returns the number of words stripped (0 when nothing qualifies).
    Guards: at least TRIM_REPEAT_MIN_WORDS, at most
    TRIM_REPEAT_MAX_WORDS, char ratio >= TRIM_REPEAT_MIN_RATIO, the
    suffix's first word must equal the opening's first word (anchors
    the strip to the bumper's start, so it cannot eat the ad's last
    word), the suffix must start after the segment's last evidence
    word, and no strip without an evidence anchor at all."""
    if not seg_words or not head_words or last_evidence_idx is None:
        return 0
    head_norm = [t for t in (_norm(w) for _, _, w in head_words) if t]
    if not head_norm:
        return 0
    seg_norm = [_norm(w) for _, _, w in seg_words]
    max_l = min(TRIM_REPEAT_MAX_WORDS, len(seg_words))
    for L in range(max_l, TRIM_REPEAT_MIN_WORDS - 1, -1):
        suffix = seg_norm[-L:]
        if not suffix or suffix[0] != head_norm[0]:
            continue
        if len(seg_words) - L <= last_evidence_idx:
            continue
        s_str = "".join(suffix)
        h_str = "".join(head_norm[:L])
        if not s_str or not h_str:
            continue
        if difflib.SequenceMatcher(None, s_str, h_str).ratio() >= \
                TRIM_REPEAT_MIN_RATIO:
            return L
    return 0


def _trim_gap(gap_words, head_words, sponsors):
    """Trim one gap's word list to the ad copy inside it.

    gap_words: [(start, end, word), ...] (Whisper words, in order).
    head_words: the episode's first words (for the repeat strip).
    sponsors: description sponsor names (may be [] — see
    _sponsor_evidence_tokens).

    Returns (start, end, note, start_idx) or None to keep the raw
    edges: start is the trimmed start (exact first ad word, no pad),
    end is floor(last ad word end) + END_PAD_SECONDS, note is a short
    string for the gap dict / log, start_idx is the trimmed first
    word's index into gap_words (for the gap's text field).

    Evidence first: the first segment with ad evidence sets the
    start, the last segment with evidence sets the end. When NO
    segment has evidence (a produced ad with no URL or sponsor word,
    e.g. the Omny Coca-Cola openers, whose descriptions name no
    sponsor either), the longest segment stands in — but only when it
    holds at least half the gap's words; a heterogeneous gap (show
    content with no ad evidence) keeps its raw edges instead of
    pretending to know where the ad is. One trailing 1-2 word segment
    is absorbed into the end (a closing CTA fragment split off by a
    long pause before it); a 3+ word tail is real content and stays
    excluded."""
    if not gap_words:
        return None
    full_names, sponsor_words = _sponsor_evidence_tokens(sponsors)
    segs = _split_segments(gap_words)
    scored = []
    for seg in segs:
        ev = [_word_evidence(w, _norm(w), full_names, sponsor_words)
              for _, _, w in seg]
        last_ev = max((i for i, v in enumerate(ev) if v > 0),
                      default=None)
        scored.append((sum(ev), last_ev))
    with_ev = [i for i, (s, _le) in enumerate(scored) if s > 0]
    if with_ev:
        si, ei = with_ev[0], with_ev[-1]
        note = f"evidence segs {si}-{ei} of {len(segs)}"
    else:
        longest = max(range(len(segs)), key=lambda i: len(segs[i]))
        if len(segs[longest]) * 2 < len(gap_words):
            return None  # no evidence, no dominant segment: keep raw
        si = ei = longest
        note = f"fallback longest seg {longest} of {len(segs)}"
    # Trailing straggler: a 1-2 word segment after the ad's last
    # evidence segment is usually the ad's closing CTA fragment
    # ("Visit" before "betterhelp.com") split off by a long pause
    # before it — on Wild at Heart the pause is 7.9s. Absorb one such
    # segment into the end (never more than one, never more than 2
    # words: a 3+ word tail is real content, e.g. the "until next
    # time" sign-off, and stays excluded; the 875 ad2 show tail is 5
    # words and 38s away, so it is excluded on both counts).
    if ei + 1 < len(segs) and len(segs[ei + 1]) <= 2:
        ei += 1
        note += ", +straggler"
    start_idx = sum(len(s) for s in segs[:si])
    start = segs[si][0][0]
    end_seg = segs[ei]
    strip = _strip_trailing_repeat(end_seg, head_words, scored[ei][1])
    if strip:
        end = end_seg[-strip - 1][1]
        note += f", strip {strip}w repeat"
    else:
        end = end_seg[-1][1]
    if start is None or end is None or end <= start:
        return None
    # End: floor to the whole second (the clip convention the pad was
    # measured on), then the +0.50s pad on the trimmed end only.
    end = math.floor(end) + END_PAD_SECONDS
    if end <= start:
        return None
    return (round(start, 1), round(end, 1), note, start_idx)


def find_gaps(whisper_words, pub_words, sponsors=None):
    """Align Whisper words against publisher words and find gap spans.

    whisper_words / pub_words: [(start, end, word), ...] in pipeline
    format (publisher start/end may be None for untimed transcripts).
    sponsors: description sponsor names (list of strings, may be
    None/[] — see _trim_gap); passed through to the trim.

    Returns a dict: {"status": "ok"|"unreliable", "coverage": float,
    "n_anchors": int, "gaps": [gap, ...]} where each gap is
    {"start", "end", "words", "duration", "offset_confirmed",
    "text": first ~12 words, "raw_start", "raw_end", "trimmed",
    "trim_note"}. start/end are the TRIMMED edges (see _trim_gap);
    raw_start/raw_end keep the untrimmed alignment edges; duration
    is measured on the trimmed span; "words" is still the raw
    Whisper-only count (the floors are checked before trimming).
    No spans at all when coverage is below MIN_COVERAGE.
    """
    ta, ka = _norm_tokens(whisper_words)
    tb, kb = _norm_tokens(pub_words)
    if not ta or not tb:
        return {"status": "unreliable", "coverage": 0.0,
                "n_anchors": 0, "gaps": []}
    chain, _n_cand = _anchors(ta, tb)
    regions = _diff_regions(ta, tb, chain)
    runs = _whisper_only_runs(regions, len(ta))
    matched = len(ta) - sum(a1 - a0 for a0, a1 in runs)
    coverage = matched / len(ta)
    if coverage < MIN_COVERAGE:
        return {"status": "unreliable", "coverage": round(coverage, 4),
                "n_anchors": len(chain), "gaps": []}
    # A run is a gap candidate when it meets both size floors; merge
    # candidates within MERGE_GAP_SECONDS of each other first, then
    # re-check the floors on the merged span (a merged span can only
    # grow, so a sub-floor pair can jointly qualify — intended: one
    # ad split by a coincidental matching phrase is still one ad).
    cands = []
    for (a0, a1) in runs:
        t0 = whisper_words[ka[a0]][0]
        t1 = whisper_words[ka[a1 - 1]][1]
        cands.append([a0, a1, t0, t1, a1 - a0])  # last: Whisper-only words
    merged = []
    for c in cands:
        if merged and c[2] - merged[-1][3] <= MERGE_GAP_SECONDS:
            merged[-1][1] = c[1]
            merged[-1][3] = c[3]
            merged[-1][4] += c[4]
        else:
            merged.append(list(c))
    whisper_only = [False] * len(ta)
    for (a0, a1, _b0, _b1) in regions:
        for i in range(a0, a1):
            whisper_only[i] = True
    gaps = []
    for (a0, a1, t0, t1, n_only) in merged:
        n_words = n_only  # Whisper-only words; the merged span may also
        # contain a few matched bridge words, which set the span's
        # clock edges but are not counted here.
        dur = t1 - t0
        if n_words < MIN_GAP_WORDS or dur < MIN_GAP_SECONDS:
            continue
        text = " ".join(whisper_words[ka[i]][2].strip()
                        for i in range(a0, min(a1, a0 + 12)))
        gap = {
            "start": round(t0, 1),
            "end": round(t1, 1),
            "words": n_words,
            "duration": round(dur, 1),
            "offset_confirmed": bool(_offset_confirmed(
                a0, a1, whisper_only, ta, tb, chain,
                pub_words, kb, whisper_words, ka)),
            "text": text,
        }
        # Trim (2026-11-13): the raw edges over-extend into adjacent
        # non-ad material the publisher also omits, so find the ad
        # copy inside the gap. The floors above are checked on the
        # raw span; the trim only moves edges, never creates or
        # drops a gap. A gap the trim cannot place keeps its raw
        # edges (trimmed=False) — a gap alone never marks an ad
        # either way.
        gap_words = [whisper_words[ka[i]] for i in range(a0, a1)]
        gap["raw_start"] = gap["start"]
        gap["raw_end"] = gap["end"]
        trimmed = _trim_gap(gap_words, whisper_words[:12], sponsors)
        if trimmed is not None:
            ts, te, note, start_idx = trimmed
            gap["start"] = ts
            gap["end"] = te
            gap["duration"] = round(te - ts, 1)
            gap["trimmed"] = True
            gap["trim_note"] = note
            gap["text"] = " ".join(
                w[2].strip()
                for w in gap_words[start_idx:start_idx + 12])
        else:
            gap["trimmed"] = False
            gap["trim_note"] = "kept raw (no evidence, no dominant segment)"
        gaps.append(gap)
    return {"status": "ok", "coverage": round(coverage, 4),
            "n_anchors": len(chain), "gaps": gaps}


# ---------------------------------------------------------------------------
# Corroboration / held markers (the only way gaps touch detection)
# ---------------------------------------------------------------------------

def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def corroborate_blocks(gaps, blocks):
    """Apply gaps to already-detected blocks.

    blocks: the detect_ad_blocks return value — (start, end, label,
    start_sharpness, end_sharpness) tuples. A gap corroborates a
    block when their overlap is >= 50% of the gap's length or >= 50%
    of the block's length (MinusPod's "at least half" rule, applied
    symmetrically so a long gap can corroborate a short block inside
    it and vice versa).

    Returns {"corroborated": [{"block_index", "gap", "boost",
    "offset_confirmed"}], "held": [gap, ...]}. A gap corroborating
    more than one block is recorded against each (rare: adjacent
    blocks); a gap corroborating none becomes a HELD marker — a
    suspect for the reel, never a chapter. This function never
    creates, moves, or deletes a block.
    """
    corroborated = []
    held = []
    for gap in gaps:
        hit = False
        for i, (bs, be, _label, _ss, _es) in enumerate(blocks):
            ov = _overlap(gap["start"], gap["end"], bs, be)
            if ov >= 0.5 * (gap["end"] - gap["start"]) or \
                    ov >= 0.5 * (be - bs):
                hit = True
                boost = (CORROBORATE_BOOST_CONFIRMED
                         if gap.get("offset_confirmed")
                         else CORROBORATE_BOOST)
                corroborated.append({
                    "block_index": i,
                    "gap": gap,
                    "boost": boost,
                    "offset_confirmed": bool(gap.get("offset_confirmed")),
                })
        if not hit:
            held.append(gap)
    return {"corroborated": corroborated, "held": held}


def held_suspects(gaps, blocks):
    """Held gaps as AdChisel suspects: (start, end, label) tuples for
    the suspect reel. Only gaps with no corroborated block qualify."""
    return [(g["start"], g["end"], "transcript-diff:held")
            for g in corroborate_blocks(gaps, blocks)["held"]]


# ---------------------------------------------------------------------------
# Top-level entry point (graceful degradation by construction)
# ---------------------------------------------------------------------------

def run_transcript_diff(whisper_words, item_body_xml, work_dir=None,
                        enabled=True, fetch_fn=None, sponsors=None):
    """Run the whole stage for one episode. Never raises.

    whisper_words: pipeline-format [(start, end, word), ...].
    item_body_xml: the raw RSS <item> body (for the transcript tag),
        or an already-parsed tag list as returned by
        extract_transcript_tags() (the form process_episode holds —
        see INTEGRATION.md; a list is used as-is).
    work_dir: when given, a stored ok result for the same transcript
        URL younger than 24h is reused (MinusPod's re-detection rule);
        fresh results are stored there as transcript_diff.json.
    enabled: per-podcast config override (False skips the stage).
    fetch_fn: injectable fetch (tests); defaults to fetch_transcript.
    sponsors: description sponsor names (list of strings, from
        extract_sponsors on the episode description — see
        INTEGRATION.md); passed to find_gaps for the trim. May be
        None/[] — the trim then runs on generic evidence alone.

    Returns a result dict with keys: status ("ok" | "skipped" |
    "error" | "unreliable"), transcript_url, transcript_type,
    coverage, n_anchors, gaps, corroborated (filled in by the caller
    via corroborate_blocks — see INTEGRATION.md), error (when status
    is "error"). Gaps are [] unless status is "ok".
    """
    result = {"status": "skipped", "transcript_url": None,
              "transcript_type": None, "coverage": None,
              "n_anchors": 0, "gaps": [], "error": None}
    if not enabled:
        return result
    try:
        if isinstance(item_body_xml, list):
            tags = item_body_xml
        else:
            tags = extract_transcript_tags(item_body_xml or "")
    except Exception as e:  # malformed item XML must not fail the run
        result.update(status="error", error=f"tag parse failed: {e}")
        return result
    best = best_transcript(tags)
    if best is None:
        return result  # skipped: no podcast:transcript tag
    result["transcript_url"] = best["url"]
    result["transcript_type"] = best["type"] or None
    cache_path = (os.path.join(work_dir, "transcript_diff.json")
                  if work_dir else None)
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if cached.get("transcript_url") == best["url"] and \
                    cached.get("status") == "ok" and \
                    time.time() - cached.get("fetched_at", 0) < \
                    CACHE_MAX_AGE_SECONDS:
                cached["from_cache"] = True
                return cached
        except Exception:
            pass  # corrupt cache: fall through to a fresh fetch
    fetch = fetch_fn or fetch_transcript
    try:
        data = fetch(best["url"])
        pub_words = parse_transcript(data, best["type"])
    except Exception as e:
        result.update(status="error", error=f"{type(e).__name__}: {e}")
        return result
    try:
        found = find_gaps(whisper_words, pub_words, sponsors=sponsors)
    except Exception as e:
        result.update(status="error", error=f"alignment failed: {e}")
        return result
    result.update(status=found["status"], coverage=found["coverage"],
                  n_anchors=found["n_anchors"], gaps=found["gaps"])
    if cache_path and result["status"] == "ok":
        try:
            os.makedirs(work_dir, exist_ok=True)
            stored = dict(result)
            stored["fetched_at"] = time.time()
            with open(cache_path, "w") as f:
                json.dump(stored, f, indent=2)
        except Exception:
            pass  # cache write failure must not fail the run
    return result
