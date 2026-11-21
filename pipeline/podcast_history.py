#!/usr/bin/env python3
"""Per-podcast ad history for AdChisel: one CSV per podcast, one row per ad.

This module is the collection + read side of the learning component
(owner's idea, 2026-11-14). It is deliberately NOT a voter: it records
what the detector found and summarises the per-show pattern. A future
voter would read these files and stay silent under 5 episodes.

File layout: <history_dir>/<podcast-id>.csv, one row per detected ad.
Columns (19):
  episode_id, episode_title, episode_duration_s, ad_index,
  sponsor, ad_type, start_s, end_s, duration_s, start_pct, end_pct,
  start_sharpness, end_sharpness, methods, pub_date, pipeline_version,
  verified, verified_source, processed_at

Rules (see history/README.md for the full format document):
- ad_index is 1-based in time order within the episode, counting AD
  rows only (dynamic + host-read). House-plug rows get ad_index 0.
- ad_type is 'dynamic' (slot-flow blocks, including slots merged into a
  detector block), 'host-read' (detector blocks, including rescues), or
  'house-plug' (a block whose sponsor is a configured house plug).
- sponsor is the block label's sponsor candidate (the part after the
  last ':'), as spoken -- not the resolved display name -- so backfill
  rows and live rows use the same rule. Empty when the block names no
  sponsor (an unnamed dynamic slot).
- methods is the label's method list '+'-joined, with '+slot' appended
  for slot-derived blocks, '+rescue' for rescued blocks, and
  '+transcript-diff' for corroborated blocks. Cross-episode
  recurrence (xep) is NOT tagged: it extends an already-detected
  block's span, it does not vote for the block, so it leaves no trace
  in the label or source.
- start_pct / end_pct are percentages of the episode duration.
- verified is true only when the row was confirmed against ground
  truth (owner marking, SponsorBlock match, hand check). verified_source
  names which one. Default false / empty.
- An episode with no detected ads gets ONE zero-ad row (ad_index 0,
  ad fields blank) so the average ad count stays honest. Rows are
  never written for a failed run: the collector is called only after a
  successful detection.
- Re-processing an episode REPLACES its rows (M30, 2026-11-19):
  append_episode deletes the episode's existing rows and writes the
  new ones in one locked atomic rewrite, stamped with the new
  processed_at / pipeline version. History is a record of what the
  pipeline currently believes, not of its first run.
- The loader EXCLUDES house-plug rows from all ad averages and slot
  statistics (they are counted separately). Zero-ad rows count toward
  the episode count but not toward slot statistics.
- Roll class is derived, never stored: pre-roll when start_pct < 5,
  post-roll when end_pct > 95, else mid-roll. Total ad time per
  episode is derived the same way (sum of durations).
- The history threshold is 5 EPISODES (distinct episode_ids) in the
  file. Below that a future voter stays silent; the loader just
  reports the count.
"""

import contextlib
import csv
import datetime
import fcntl
import os
import statistics
import tempfile
from email.utils import parsedate_to_datetime

COLUMNS = [
    "episode_id", "episode_title", "episode_duration_s", "ad_index",
    "sponsor", "ad_type", "start_s", "end_s", "duration_s",
    "start_pct", "end_pct", "start_sharpness", "end_sharpness",
    "methods", "pub_date", "pipeline_version", "verified",
    "verified_source", "processed_at",
]

AD_TYPES = ("dynamic", "host-read", "house-plug")
MIN_EPISODES = 5  # a future voter stays silent below this many episodes
PRE_ROLL_MAX_PCT = 5.0   # start_pct below this: pre-roll
POST_ROLL_MIN_PCT = 95.0  # end_pct above this: post-roll


def default_history_dir():
    """<podcast-ads root>/history. Found by walking up from this file
    until a directory containing podcasts.yaml; falls back to the
    parent of this file's directory (the pipeline/ layout)."""
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    while True:
        if os.path.exists(os.path.join(d, "podcasts.yaml")):
            return os.path.join(d, "history")
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.join(os.path.dirname(here), "history")
        d = parent


def history_path(history_dir, podcast_id):
    return os.path.join(history_dir, podcast_id + ".csv")


# ---------------------------------------------------------------- helpers

def parse_pub_date(raw):
    """RFC 2822 pubDate (or YYYY-MM-DD) -> 'YYYY-MM-DD', '' on failure."""
    if not raw:
        return ""
    raw = str(raw).strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    try:
        dt = parsedate_to_datetime(raw)
        return dt.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def sponsor_candidate(label):
    """Sponsor candidate from a block label ('methods:sponsor')."""
    if not label:
        return ""
    cand = label.rsplit(":", 1)[-1].strip()
    return cand


def methods_from_label(label, source):
    """'+'-joined method list for a block, from its label and source."""
    methods = []
    if label and ":" in label:
        methods = [m for m in label.rsplit(":", 1)[0].split("/") if m]
    src = source or ""
    if src in ("slot", "merged") and "slot" not in methods:
        methods.append("slot")
    if src == "rescue" and "rescue" not in methods:
        methods.append("rescue")
    if "transcript-diff" in src and "transcript-diff" not in methods:
        methods.append("transcript-diff")
    if not methods and src in ("slot", "merged"):
        methods = ["slot"]
    return "+".join(methods)


def _norm_name(name):
    return (name or "").strip().lower()


def is_house_plug_candidate(candidate, house_plugs):
    """True when a sponsor candidate is a configured house plug.

    Mirrors run_pipeline.is_house_plug, but matches the raw candidate
    against labels AND variants (the live check matches the resolved
    display name against labels only). house_plugs is the config list
    of {'label': ..., 'variants': set(...)}.
    """
    cand = _norm_name(candidate)
    if not cand or not house_plugs:
        return False
    for hp in house_plugs:
        names = {_norm_name(hp.get("label", ""))}
        names |= {_norm_name(v) for v in hp.get("variants", set())}
        names.discard("")
        if cand in names:
            return True
    return False


def classify_roll(start_pct, end_pct):
    """Derived roll class: pre / mid / post. Never stored in the file."""
    if start_pct is not None and start_pct < PRE_ROLL_MAX_PCT:
        return "pre"
    if end_pct is not None and end_pct > POST_ROLL_MIN_PCT:
        return "post"
    return "mid"


def _fmt(x, places=1):
    if x is None:
        return ""
    return f"{round(float(x), places):.{places}f}"


# ---------------------------------------------------------------- writer

def build_rows(episode_id, episode_title, episode_duration_s, pub_date,
               blocks, sources, house_plugs, pipeline_version,
               processed_at=None, verified_map=None):
    """Build history rows for one episode from final detector blocks.

    blocks: list of (start, end, label, start_sharpness, end_sharpness).
    sources: parallel list of source strings ('detector' | 'slot' |
    'merged' | 'rescue' | '...+transcript-diff').
    verified_map: optional {(start, end): (verified_bool, source_str)}
    for rows confirmed against ground truth; default all false.
    Returns a list of row dicts (all values strings).
    """
    pub_date = parse_pub_date(pub_date)
    processed_at = processed_at or datetime.date.today().isoformat()
    dur = float(episode_duration_s) if episode_duration_s else 0.0
    verified_map = verified_map or {}
    rows = []
    ad_n = 0
    for (s, e, label, ss, es), src in zip(blocks, sources):
        s, e = float(s), float(e)
        sponsor = sponsor_candidate(label)
        if is_house_plug_candidate(sponsor, house_plugs):
            ad_type = "house-plug"
        elif (src or "") in ("slot", "merged"):
            ad_type = "dynamic"
        else:
            ad_type = "host-read"
        if ad_type == "house-plug":
            ad_index = 0
        else:
            ad_n += 1
            ad_index = ad_n
        verified, vsrc = verified_map.get((round(s, 1), round(e, 1)),
                                          (False, ""))
        rows.append({
            "episode_id": episode_id,
            "episode_title": episode_title,
            "episode_duration_s": _fmt(dur, 1),
            "ad_index": str(ad_index),
            "sponsor": sponsor,
            "ad_type": ad_type,
            "start_s": _fmt(s, 1),
            "end_s": _fmt(e, 1),
            "duration_s": _fmt(e - s, 1),
            "start_pct": _fmt(100.0 * s / dur, 2) if dur else "",
            "end_pct": _fmt(100.0 * e / dur, 2) if dur else "",
            "start_sharpness": "" if ss is None else _fmt(ss, 2),
            "end_sharpness": "" if es is None else _fmt(es, 2),
            "methods": methods_from_label(label, src),
            "pub_date": pub_date,
            "pipeline_version": pipeline_version or "",
            "verified": "true" if verified else "false",
            "verified_source": vsrc if verified else "",
            "processed_at": processed_at,
        })
    if not rows:
        # Zero-ad episode: one row so the average ad count stays honest.
        rows.append({
            "episode_id": episode_id,
            "episode_title": episode_title,
            "episode_duration_s": _fmt(dur, 1),
            "ad_index": "0",
            "sponsor": "",
            "ad_type": "",
            "start_s": "",
            "end_s": "",
            "duration_s": "",
            "start_pct": "",
            "end_pct": "",
            "start_sharpness": "",
            "end_sharpness": "",
            "methods": "",
            "pub_date": pub_date,
            "pipeline_version": pipeline_version or "",
            "verified": "false",
            "verified_source": "",
            "processed_at": processed_at,
        })
    return rows


@contextlib.contextmanager
def _history_lock(history_dir, podcast_id):
    """Exclusive per-file lock (M31, 2026-11-19).

    Held across load -> dedup/replace decision -> whole-file rewrite,
    so two concurrent writers cannot both pass the duplicate check
    and duplicate an episode's rows. The lock file lives next to the
    CSV and is never deleted."""
    os.makedirs(history_dir, exist_ok=True)
    lock_path = history_path(history_dir, podcast_id) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _title_date_key(title, pub_date):
    """(normalised title, parsed pub_date): the second dedup key (m20).

    Catches rows written under a different episode_id for the same
    episode (the live backfill rows use a slug id, the live writer
    uses the RSS guid). The date must be present on both sides, so
    undated rows never match by title alone."""
    t = (title or "").strip().lower()
    d = parse_pub_date(pub_date)
    return (t, d) if t and d else None


def _load_raw_rows(history_dir, podcast_id):
    """Raw string rows of a history CSV (no type conversion).

    Used by append_episode for the atomic rewrite: kept rows are
    written back byte-for-byte instead of round-tripping through
    load_rows(), which converts verified to bool ("True" vs the
    file's "true") and floats back to strings."""
    path = history_path(history_dir, podcast_id)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (v or "") for k, v in r.items()}
                for r in csv.DictReader(f)]


def append_episode(history_dir, podcast_id, episode_id, episode_title,
                   episode_duration_s, pub_date, blocks, sources,
                   house_plugs, pipeline_version, processed_at=None,
                   verified_map=None):
    """Append one episode's rows to history/<podcast_id>.csv.

    Never raises: any failure returns {'ok': False, 'error': ...} and
    the caller (the pipeline) logs and continues.
    Re-processing an episode REPLACES its rows (M30): the episode's
    existing rows are deleted and the new rows written in one locked
    atomic rewrite. An episode is recognised by its episode_id OR by
    (normalised title, pub_date) (m20), so rows written under a
    different id for the same episode (slug backfill vs guid writer)
    are replaced, not duplicated. T3-N6: the matched old rows'
    verified flags are carried onto the replacement rows (same
    (start, end) edges -- the key build_rows uses for its
    verified_map); a row whose edges moved gets no carried flag,
    and an explicit caller verified_map wins on collision.
    Returns {'ok', 'rows_written', 'skipped', 'error', 'replaced',
    'replaced_episode_ids'}; 'skipped' is always None now (kept in
    the return shape for callers that read it).
    """
    try:
        os.makedirs(history_dir, exist_ok=True)
        path = history_path(history_dir, podcast_id)
        with _history_lock(history_dir, podcast_id):
            existing = _load_raw_rows(history_dir, podcast_id)
            new_key = _title_date_key(episode_title, pub_date)
            matched_ids = set()
            matched = []
            kept = []
            for r in existing:
                same_id = r.get("episode_id", "") == episode_id
                r_key = _title_date_key(r.get("episode_title", ""),
                                        r.get("pub_date", ""))
                same_ep = new_key is not None and r_key == new_key
                if same_id or same_ep:
                    matched_ids.add(r.get("episode_id", ""))
                    matched.append(r)
                else:
                    kept.append(r)
            # T3-N6: carry the matched old rows' verified flags.
            carried = {}
            for r in matched:
                if (r.get("verified", "") or "").strip().lower() != "true":
                    continue
                try:
                    key = (round(float(r.get("start_s", "")), 1),
                           round(float(r.get("end_s", "")), 1))
                except (TypeError, ValueError):
                    continue
                carried[key] = (True, r.get("verified_source", ""))
            merged_map = dict(carried)
            if verified_map:
                merged_map.update(verified_map)
            rows = build_rows(episode_id, episode_title,
                              episode_duration_s, pub_date, blocks,
                              sources, house_plugs, pipeline_version,
                              processed_at, merged_map)
            # Atomic whole-file rewrite (M31): temp file in the same
            # directory + os.replace, never open(path, "a").
            fd, tmp = tempfile.mkstemp(prefix=".history-",
                                       suffix=".tmp",
                                       dir=history_dir)
            try:
                with os.fdopen(fd, "w", newline="",
                               encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=COLUMNS)
                    w.writeheader()
                    for r in kept:
                        w.writerow({c: r.get(c, "") for c in COLUMNS})
                    for r in rows:
                        w.writerow(r)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        return {"ok": True, "rows_written": len(rows),
                "skipped": None, "error": None,
                "replaced": bool(matched_ids),
                "replaced_episode_ids": sorted(matched_ids)}
    except Exception as exc:  # never fail an episode run on history
        return {"ok": False, "rows_written": 0,
                "skipped": None, "error": repr(exc),
                "replaced": False, "replaced_episode_ids": []}


# ---------------------------------------------------------------- reader

def _to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _to_int(v, default=0):
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def load_rows(history_dir, podcast_id):
    """Read a show's history file. Missing file -> []. House-plug rows
    are returned with the rest; summarize() excludes them from ad stats.
    """
    path = history_path(history_dir, podcast_id)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "episode_id": r.get("episode_id", ""),
                "episode_title": r.get("episode_title", ""),
                "episode_duration_s": _to_float(r.get("episode_duration_s")),
                "ad_index": _to_int(r.get("ad_index")),
                "sponsor": r.get("sponsor", ""),
                "ad_type": r.get("ad_type", ""),
                "start_s": _to_float(r.get("start_s")),
                "end_s": _to_float(r.get("end_s")),
                "duration_s": _to_float(r.get("duration_s")),
                "start_pct": _to_float(r.get("start_pct")),
                "end_pct": _to_float(r.get("end_pct")),
                "start_sharpness": _to_float(r.get("start_sharpness")),
                "end_sharpness": _to_float(r.get("end_sharpness")),
                "methods": r.get("methods", ""),
                "pub_date": r.get("pub_date", ""),
                "pipeline_version": r.get("pipeline_version", ""),
                "verified": (r.get("verified", "") or "").strip().lower()
                            == "true",
                "verified_source": r.get("verified_source", ""),
                "processed_at": r.get("processed_at", ""),
            })
    return rows


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def _std(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return 0.0 if xs else None
    return round(statistics.pstdev(xs), 2)


def _most_common(values):
    """Most common value, case-insensitive grouping, reported in its
    most common original form. Returns (value, share) or (None, 0)."""
    groups = {}
    for v in values:
        if not v:
            continue
        groups.setdefault(v.strip().lower(), []).append(v)
    if not groups:
        return None, 0.0
    key = max(groups, key=lambda k: len(groups[k]))
    forms = groups[key]
    form = max(set(forms), key=forms.count)
    return form, round(len(forms) / sum(len(g) for g in groups.values()), 2)


def _majority(values):
    if not values:
        return None, 0.0
    top, share = _most_common(values)
    return top, share


def summarize(rows, min_episodes=MIN_EPISODES):
    """Per-show summary. House-plug rows are EXCLUDED from all ad
    averages and slot statistics (counted separately). Zero-ad rows
    count toward the episode count only."""
    episodes = sorted({r["episode_id"] for r in rows if r["episode_id"]})
    ad_rows = [r for r in rows if r["ad_type"] in ("dynamic", "host-read")]
    plug_rows = [r for r in rows if r["ad_type"] == "house-plug"]
    zero_eps = [e for e in episodes
                if not any(r["episode_id"] == e for r in ad_rows)]
    counts = {}
    for r in ad_rows:
        counts[r["episode_id"]] = counts.get(r["episode_id"], 0) + 1
    count_vals = [counts.get(e, 0) for e in episodes]
    ad_time = {}
    for r in ad_rows:
        if r["duration_s"] is not None:
            ad_time[r["episode_id"]] = (ad_time.get(r["episode_id"], 0.0)
                                        + r["duration_s"])
    time_vals = [ad_time.get(e, 0.0) for e in episodes]
    slots = {}
    by_slot = {}
    for r in ad_rows:
        by_slot.setdefault(r["ad_index"], []).append(r)
    for idx in sorted(by_slot):
        rs = by_slot[idx]
        rolls = {"pre": 0, "mid": 0, "post": 0}
        for r in rs:
            rolls[classify_roll(r["start_pct"], r["end_pct"])] += 1
        maj_type, maj_share = _majority([r["ad_type"] for r in rs])
        spons, spons_share = _most_common([r["sponsor"] for r in rs])
        ss = [r["start_sharpness"] for r in rs
              if r["start_sharpness"] is not None]
        es = [r["end_sharpness"] for r in rs
              if r["end_sharpness"] is not None]
        slots[idx] = {
            "n": len(rs),
            "mean_start_pct": _mean([r["start_pct"] for r in rs]),
            "std_start_pct": _std([r["start_pct"] for r in rs]),
            "mean_duration_s": _mean([r["duration_s"] for r in rs]),
            "std_duration_s": _std([r["duration_s"] for r in rs]),
            "mean_start_sharpness": _mean(ss),
            "mean_end_sharpness": _mean(es),
            "n_sharpness": len(ss),
            "majority_type": maj_type,
            "majority_type_share": maj_share,
            "most_common_sponsor": spons,
            "sponsor_share": spons_share,
            "rolls": rolls,
        }
    pub_dates = sorted({r["pub_date"] for r in rows if r["pub_date"]})
    type_split = {}
    for r in rows:
        if r["ad_type"]:
            type_split[r["ad_type"]] = type_split.get(r["ad_type"], 0) + 1
    return {
        "episodes": len(episodes),
        "episodes_with_ads": len(episodes) - len(zero_eps),
        "episodes_zero_ad": len(zero_eps),
        "total_ad_rows": len(ad_rows),
        "house_plug_rows": len(plug_rows),
        "verified_rows": sum(1 for r in ad_rows if r["verified"]),
        "threshold_met": len(episodes) >= min_episodes,
        "threshold": min_episodes,
        "ad_count_per_episode": {
            "mean": _mean(count_vals),
            "min": min(count_vals) if count_vals else None,
            "max": max(count_vals) if count_vals else None,
            "values": count_vals,
        },
        "total_ad_time_s_per_episode": {
            "mean": _mean(time_vals),
            "min": round(min(time_vals), 1) if time_vals else None,
            "max": round(max(time_vals), 1) if time_vals else None,
        },
        "type_split": type_split,
        "date_range": {
            "first_pub": pub_dates[0] if pub_dates else None,
            "last_pub": pub_dates[-1] if pub_dates else None,
        },
        "slots": slots,
    }


def summarize_file(history_dir, podcast_id, min_episodes=MIN_EPISODES):
    """Load a show's file and summarise it. Missing file -> empty summary."""
    rows = load_rows(history_dir, podcast_id)
    s = summarize(rows, min_episodes)
    s["podcast_id"] = podcast_id
    s["rows"] = len(rows)
    return s
