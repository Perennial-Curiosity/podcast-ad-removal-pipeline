#!/usr/bin/env python3
"""Cross-episode text recurrence (positive direction only) — pipeline
port of the AdChisel xep_corroborate test (sb_eval/ab/xep_common.py,
2026-11-11, KEPT: SB recall 0.673 -> 0.701, FP 36 -> 33).

A recurring span — 8-word shingles of this episode's transcript that
appear in >= 2 prior episodes of the SAME podcast — is sponsor copy
repeated across episodes. Spans are used by _apply_xep_corroboration
in run_pipeline.py to extend blocks the detector already found; they
never create a block, never add a vote, and never add a mention.

This module never calls find_mentions, never adds sponsor variants,
and never touches the orphan path — it only reads word streams and
returns (start, end) spans. That is the orphan-flip constraint
(HANDOFF.md): any new mention source routed through find_mentions
risks flipping sponsors off the brand-orphan path and shrinking
blocks.

Differences from the SB-eval original (xep_common.py):
- Priors come from the live pipeline, not the SB-eval cache: prior
  episodes are the other episode work dirs under the podcast's
  episodes dir (podcasts/<id>/episodes/<slug>/), each holding the
  transcript.json that transcribe_chunked writes. One podcast is one
  show, so there is no show-grouping step.
- The span cache lives in the current episode's work dir as
  xep_spans.json (episode scratch, like chunk_*.json), keyed the same
  way (prior + self transcript.json mtimes/sizes). A new prior
  episode, or a re-transcription, invalidates it.
- A show with < 2 prior transcribed episodes yields no spans (the
  >= 2 priors threshold is never relaxed); the first two episodes of
  a podcast are therefore unaffected by this stage.
"""
import json
import os
import re

SHINGLE_SIZE = 8
MIN_PRIOR_EPISODES = 2
MIN_SPAN_SECONDS = 5.0
MAX_GAP_TOKENS = 4  # bridge short mistranscription/ad-lib gaps

_WORD_RE = re.compile(r"[a-z0-9]+")


def norm_tokens(word):
    """Lowercased alphanumeric tokens of one Whisper word (same regex
    as the MinusPod prototype; "isn't" -> ["isn", "t"])."""
    return _WORD_RE.findall(word.lower())


def token_stream(words):
    """[(token, word_start, word_end)] over the word list, skipping
    words that yield no tokens (pure punctuation)."""
    toks = []
    for s, e, w in words:
        for t in norm_tokens(w):
            toks.append((t, s, e))
    return toks


def shingle_set(words):
    """Distinct 8-word shingles of one episode (set-deduped, so
    repetition inside a single episode cannot satisfy the
    cross-episode threshold)."""
    toks = token_stream(words)
    ws = [t for t, _, _ in toks]
    n = len(ws)
    if n < SHINGLE_SIZE:
        return set()
    return {" ".join(ws[i:i + SHINGLE_SIZE])
            for i in range(n - SHINGLE_SIZE + 1)}


def load_words(ep_dir, eid):
    """Word list [(start, end, text)] for one episode work dir."""
    tdata = json.load(open(os.path.join(ep_dir, eid, "transcript.json")))
    return [(w[0], w[1], w[2]) for w in tdata["words"]]


def prior_episodes(ep_dir, slug):
    """Episode work dirs under ep_dir with a transcript.json,
    excluding the current episode."""
    out = []
    for d in sorted(os.listdir(ep_dir)):
        if d == slug:
            continue
        if not os.path.isdir(os.path.join(ep_dir, d)):
            continue
        if os.path.exists(os.path.join(ep_dir, d, "transcript.json")):
            out.append(d)
    return out


def find_recurring_spans(words, prior_word_lists,
                         min_priors=MIN_PRIOR_EPISODES):
    """Spans of `words` whose 8-word shingles appear in >= min_priors
    prior episodes. Returns [{start, end, n_tokens, frac}] sorted by
    start. `prior_word_lists` is a list of word lists, one per prior
    episode."""
    if len(prior_word_lists) < min_priors or not words:
        return []
    prior_shingles = [shingle_set(pw) for pw in prior_word_lists]
    toks = token_stream(words)
    if len(toks) < SHINGLE_SIZE:
        return []
    ws = [t for t, _, _ in toks]
    recurring = [False] * len(toks)
    for i in range(len(toks) - SHINGLE_SIZE + 1):
        sh = " ".join(ws[i:i + SHINGLE_SIZE])
        hits = sum(1 for ps in prior_shingles if sh in ps)
        if hits >= min_priors:
            for j in range(i, i + SHINGLE_SIZE):
                recurring[j] = True
    # Merge runs, bridging gaps of <= MAX_GAP_TOKENS non-recurring tokens.
    spans = []
    i = 0
    n = len(toks)
    while i < n:
        if not recurring[i]:
            i += 1
            continue
        j = i
        gap = 0
        last_rec = i
        while j + 1 < n:
            if recurring[j + 1]:
                j += 1
                last_rec = j
                gap = 0
            elif gap < MAX_GAP_TOKENS:
                j += 1
                gap += 1
            else:
                break
        # run is toks[i..last_rec]
        start = toks[i][1]
        end = toks[last_rec][2]
        if end - start >= MIN_SPAN_SECONDS:
            n_tok = last_rec - i + 1
            frac = sum(recurring[i:last_rec + 1]) / n_tok
            spans.append({"start": round(start, 1), "end": round(end, 1),
                          "n_tokens": n_tok,
                          "recurring_frac": round(frac, 2)})
        i = last_rec + 1
    return spans


def _cache_key(ep_dir, slug, priors):
    key = [slug]
    for p in priors:
        st = os.stat(os.path.join(ep_dir, p, "transcript.json"))
        key.append(f"{p}:{st.st_mtime}:{st.st_size}")
    st = os.stat(os.path.join(ep_dir, slug, "transcript.json"))
    key.append(f"self:{st.st_mtime}:{st.st_size}")
    return "|".join(key)


def recurring_spans_for_episode(ep_dir, slug, use_cache=True):
    """Recurring spans for episode `slug` in podcast dir `ep_dir`
    (cached as <ep_dir>/<slug>/xep_spans.json).
    Returns (spans, priors)."""
    priors = prior_episodes(ep_dir, slug)
    if len(priors) < MIN_PRIOR_EPISODES:
        return [], priors
    work_dir = os.path.join(ep_dir, slug)
    cp = os.path.join(work_dir, "xep_spans.json")
    if use_cache and os.path.exists(cp):
        try:
            c = json.load(open(cp))
            if c.get("cache_key") == _cache_key(ep_dir, slug, priors):
                return c["spans"], c["priors"]
        except Exception:
            pass
    words = load_words(ep_dir, slug)
    prior_words = [load_words(ep_dir, p) for p in priors]
    spans = find_recurring_spans(words, prior_words)
    try:
        json.dump({"episode": slug, "priors": priors,
                   "cache_key": _cache_key(ep_dir, slug, priors),
                   "spans": spans},
                  open(cp, "w"), indent=1)
    except Exception:
        pass  # cache is an optimization; a failed write is not fatal
    return spans, priors
