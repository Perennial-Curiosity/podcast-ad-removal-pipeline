#!/usr/bin/env python3
"""
Podcast ad-removal pipeline (podcast-agnostic).

Each run (per podcast, via --podcast <id>):
  1. Fetches the podcast's RSS feed
  2. Finds episodes not yet processed (per-podcast state tracks GUIDs)
  3. Downloads the episode MP3
  4. Transcribes with faster-whisper (chunked, resumable)
  5. Extracts sponsor brands from the RSS description (ground truth)
  6. Locates each sponsor's ad-read block in the transcript
  7. Marks each detected ad as a chapter ("Muse Detected Ad For <name>")
     in the full original audio -- nothing is cut. House plugs (a show's
     own platforms/memberships, e.g. Pepperbox, Club TWiT) are a subtype
     of advertisement: detected and marked the same way, but titled
     "Muse Detected House Plug For <name>", never as an ad
  8. Keeps pre-existing chapters (or auto-generates from topic changes
     if none exist), split at ad boundaries
  9. Builds a suspect reel for low-confidence segments (review only)
 10. Writes a per-episode report and regenerates the RSS feed

Podcasts are configured in ~/workspace/podcast-ads/podcasts.yaml —
no code changes needed to add one.

Safety: conservative marking only. A block is marked only when a sponsor brand is
mentioned AND an opening cue phrase precedes it AND a closing boundary
(silence gap or return cue) is found within sane duration bounds. If detection
is uncertain for an episode, the episode is left unmarked and flagged.
"""

import contextlib
import difflib
import fcntl
import glob
import hashlib
import json
import os
import random
import re
from datetime import date, datetime, timezone
import subprocess
import sys
import threading
import time
import urllib.request
from html import unescape

# The model snapshot lives in the default HF cache; force offline so the
# (broken-proxy) network is never touched.
os.environ["HF_HUB_OFFLINE"] = "1"

BASE_DIR = os.path.expanduser("~/workspace/podcast-ads")
CONFIG_PATH = os.path.join(BASE_DIR, "podcasts.yaml")

# Ad-block bounds (seconds)
MIN_AD_LEN = 45
# Heat-map blocks need only 2+ agreeing votes, so they may be shorter
# than single-method blocks: dynamic spots run 30-31s (Unsubscribe 286,
# 2026-10-26). Single-method blocks keep the 45s floor.
HEATMAP_MIN_AD_LEN = 30
MAX_AD_LEN = 12 * 60
# T2-N7 guard: methods that reflect spoken ad content. A heat-map block
# that only reaches the floor because of votes past the last transcribed
# word (audio-horizon extension) must carry one of these; url / voice /
# reverb / history-preroll alone are not content evidence — a spoken URL
# can be the show's own site, voice/reverb are delivery anomalies, and
# history-preroll is corroboration from other episodes, not evidence in
# this episode's audio.
_CONTENT_METHODS = frozenset({'cta', 'brand', 'cluster', 'cluster-popular',
                              'duplicate', 'chapter', 'brand-orphan'})
# Orphan-mention gap split (M11, revised 2026-11-20): mentions more
# than one segment-length apart belong to different reads / content
# mentions, not to one window. 180s is the segment span cap the URL
# grouping already uses.
ORPHAN_GAP_MAX = 180.0

# Shared spoken-URL TLD set (M15): the single canonical list.
# find_url_positions, extract_sponsors, the single-cluster anchor check,
# sponsor_root, split_rescue._TLDS and transcript_diff.TRIM_TLDS must
# all equal this set — a missing TLD is a silent miss (SN 1101,
# 2026-10-22: ".tools"). Keep in sync; the fix-round driver asserts
# the sets are equal.
URL_TLDS = {"com", "net", "org", "io", "ai", "tv", "co", "us", "tools",
            "app", "dev", "xyz", "online", "store", "tech", "site",
            "cloud", "me", "cc", "gg", "life", "world", "news", "media",
            "agency", "digital", "solutions", "systems", "network",
            "security"}
URL_TLD_RE = r"\.(" + "|".join(sorted(URL_TLDS, key=len, reverse=True)) + r")$"

# History pre-roll voter (2026-11-14, A/B test — NOT merged): the
# harness sets HISTORY_PREROLL_WINDOW to (start_s, end_s) or None
# per episode. The window is where this show's first ad usually
# starts (leave-one-out prior from the per-podcast history file).
HISTORY_PREROLL_WINDOW = None
# Chunk size for transcription (seconds) — keeps each job short
TRANSCRIBE_CHUNK = 180

# Dynamic slots (2026-11-11, DESIGN.md): a second download of the same
# episode, aligned against the first, reveals dynamic-ad slots — the
# regions where the two copies differ. Integrated as a boundary source
# for dynamic ads, not a standalone detector.
SLOT_ALIGNER_VERSION = "v5b"   # frozen aligner params live in slot_align.py
SLOT_PROBE_RECHECK_DAYS = 30   # re-probe a feed this often
SLOT_MAX_ATTEMPTS = 3          # B-download retries before giving up
SLOT_GUARD_MAX_DIFF_RATE = 0.25  # differing-token rate above this: abort slots
SLOT_GUARD_MAX_DUR_RATIO = 0.10  # |B dur - A dur| / A dur above this: abort
MAX_UNVERIFIED_PIN_USES = 5  # (N8) reuses of a pinned B whose download
# was never Content-Length verified before the pin is dropped and a
# fresh B downloaded. Verified pins never expire on uses.
SLOT_GUARD_MAX_SLOT_SHARE = 0.25  # gated slots > 25% of episode: abort
SLOT_B_MARGIN_S = 10.0         # B-side gate window margin around mapped B span
SLOT_FLOOR_DENSITY_MIN = 0.5  # standalone slot density below this: drop (not an ad read)
SLOT_PINNED_B_KEEP_SIBLINGS = 4  # pinned B mp3s kept per podcast
# besides the current episode (current + 4 siblings = 5 max,
# ~1.25 GB at ~250 MB per B). Older pinned Bs are pruned at the
# next B flow; a pruned episode re-downloads a fresh B on its
# next run (old behaviour).
# Transcribe-config fingerprint (2026-11-11): a stored config that differs
# from the current one invalidates a cached chunk transcript. Missing
# config = legacy cache (all pre-2026-11-11 caches are base/en/beam5).
TRANSCRIBE_CONFIG = {"model": "base", "language": "en", "beam_size": 5}

_MODEL_REVISION = None


def _model_revision():
    """Revision/hash of the faster-whisper `base` weights in the local HF
    cache, or "unknown" when it cannot be determined (no cache, several
    snapshots). Used only as a cache-invalidation signal, never as a
    load path: load_model() still loads by name, offline. (m4)"""
    global _MODEL_REVISION
    if _MODEL_REVISION is None:
        _MODEL_REVISION = "unknown"
        try:
            snap_dir = os.path.join(
                os.path.expanduser("~/.cache/huggingface/hub"),
                "models--Systran--faster-whisper-base", "snapshots")
            snaps = [d for d in os.listdir(snap_dir)
                     if os.path.isdir(os.path.join(snap_dir, d))]
            if len(snaps) == 1:
                _MODEL_REVISION = snaps[0]
        except OSError:
            pass
    return _MODEL_REVISION


def _current_transcribe_config():
    """TRANSCRIBE_CONFIG plus the model revision (m4)."""
    cfg = dict(TRANSCRIBE_CONFIG)
    cfg["model_revision"] = _model_revision()
    return cfg


def _transcribe_config_matches(stored):
    """True when a stored chunk config matches the current one. Base
    fields must match exactly. The model revision only invalidates
    when BOTH sides know it and they differ: pre-patch chunks carry no
    revision and are accepted (their weights predate tracking)."""
    if not isinstance(stored, dict):
        return False
    for k, v in TRANSCRIBE_CONFIG.items():
        if stored.get(k, v) != v:
            return False
    stored_rev = stored.get("model_revision", "unknown")
    current_rev = _model_revision()
    if stored_rev in (None, "unknown") or current_rev == "unknown":
        return True
    return stored_rev == current_rev

# Popular podcast sponsors not in the episode description.
# Used ONLY by the cluster finder (4+ mentions in 1 min) as a safety net
# for unlisted/dynamic ads — never by the brand matcher. Hits here are
# low-confidence: auto-cut only with 2+ method votes (heat map).
# Variants include common Whisper mistranscriptions (e.g. mitt->mint).
POPULAR_BRANDS = [
    {"label": "Mint Mobile", "variants": ["mint mobile", "mint", "mitt mobile", "mitt"]},
    {"label": "BetterHelp", "variants": ["betterhelp", "better help"]},
    {"label": "Shopify", "variants": ["shopify"]},
    {"label": "Squarespace", "variants": ["squarespace", "square space"]},
    {"label": "Indeed", "variants": ["indeed"]},
    {"label": "LinkedIn", "variants": ["linkedin", "linked in"]},
    {"label": "Aura", "variants": ["aura"]},
    {"label": "Rocket Money", "variants": ["rocket money"]},
    {"label": "HelloFresh", "variants": ["hellofresh", "hello fresh"]},
    {"label": "Factor", "variants": ["factor", "factor meals"]},
    {"label": "AG1", "variants": ["ag1", "ag 1", "athletic greens"]},
    {"label": "Manscaped", "variants": ["manscaped", "man scaped"]},
    {"label": "Seed", "variants": ["seed", "seed probiotic"]},
    {"label": "Zocdoc", "variants": ["zocdoc", "zoc doc"]},
    {"label": "Progressive", "variants": ["progressive"]},
    {"label": "SimpliSafe", "variants": ["simplisafe", "simpli safe"]},
    {"label": "Quince", "variants": ["quince"]},
]

# Chapter-title ad markers (method 'chapter', added 2026-10-14 per the owner —
# idea borrowed from the podcast-ad-cleaner project). Some podcasts label ad
# segments directly in their chapter titles ("[Ad]", "Ad:", "Sponsor", ...).
# A publisher-labeled ad chapter is high-precision, so it votes like the
# other high-precision methods and is cut even on a single vote.
# Deliberately does NOT match "advertising" (industry discussion).
CHAPTER_AD_PATTERNS = [
    r'\[\s*ads?\s*\]',          # [Ad], [Ads]
    r'^ads?\s*[:\-–—]',         # "Ad: ...", "Ads - ..."
    r'\badvertisements?\b',     # Advertisement(s)
    r'\bsponsors?(hip|ed)?\b',  # Sponsor, Sponsors, Sponsored, Sponsorship
    r'\bcommercials?(\s+break)?\b',  # Commercial, Commercial break
    r'\bad\s+break\b',          # Ad break
    r'\badbreak\b',
]


def load_config(podcast_id):
    """Load podcasts.yaml and return (global_cfg, podcast_cfg)."""
    import yaml
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    pcfg = cfg["podcasts"].get(podcast_id)
    if not pcfg:
        raise SystemExit(f"unknown podcast id '{podcast_id}' in {CONFIG_PATH}")
    return cfg, pcfg


def podcast_paths(podcast_id):
    """Per-podcast working paths."""
    root = os.path.join(BASE_DIR, "podcasts", podcast_id)
    return {
        "root": root,
        "ep_dir": os.path.join(root, "episodes"),
        "site_dir": os.path.join(root, "feed-site"),
        "state_path": os.path.join(root, "state.json"),
    }


def house_plugs(pcfg):
    """House plugs configured for this podcast, as {label, variants} set."""
    return [{"label": p["label"], "variants": set(p["variants"])}
            for p in pcfg.get("house_plugs", [])]


def github_feed_url(cfg, pcfg):
    return (f"https://raw.githubusercontent.com/{cfg['github_owner']}/"
            f"{cfg['github_repo']}/main/{pcfg['github_feed_path']}")


# Per-podcast globals, populated by main() from podcasts.yaml.
# (Kept as globals so the pipeline functions stay unchanged.)
FEED_URL = None
EP_DIR = None
SITE_DIR = None
STATE_PATH = None
HOUSE_PLUGS = []
GITHUB_FEED_URL = None
PODCAST_ID = None


def init_podcast(podcast_id):
    """Load config and set per-podcast globals. Returns (cfg, pcfg)."""
    global FEED_URL, EP_DIR, SITE_DIR, STATE_PATH, HOUSE_PLUGS, GITHUB_FEED_URL, PODCAST_ID
    cfg, pcfg = load_config(podcast_id)
    paths = podcast_paths(podcast_id)
    FEED_URL = pcfg["feed_url"]
    EP_DIR = paths["ep_dir"]
    SITE_DIR = paths["site_dir"]
    STATE_PATH = paths["state_path"]
    HOUSE_PLUGS = house_plugs(pcfg)
    GITHUB_FEED_URL = github_feed_url(cfg, pcfg)
    PODCAST_ID = podcast_id
    for d in (paths["root"], EP_DIR, SITE_DIR):
        os.makedirs(d, exist_ok=True)
    return cfg, pcfg

OPEN_CUES = [
    "brought to you by",
    "brought to you",
    "sponsor",
    "advertiser",
    "a word from",
    "this episode is brought",
    "this portion of",
    "today's sponsor",
]
RETURN_CUES = [
    "welcome back",
    "we're back",
    "we are back",
    "all right",
    "alright",
]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _validate_feed_xml(text):
    """Reject a truncated/invalid feed body (fix M35, 2026-11-19).
    A proxy stall that closes cleanly mid-body used to be returned as
    a complete feed; parse_items() then silently yielded fewer items.
    Raises ValueError on any failure so the caller retries / fails
    loudly instead of processing a short feed as if complete."""
    import xml.etree.ElementTree as _ET
    if not text or "</rss>" not in text:
        raise ValueError("feed body is truncated (no closing </rss>)")
    try:
        root = _ET.fromstring(text)
    except _ET.ParseError as e:
        raise ValueError(f"feed body is not well-formed XML: {e}")
    if root.tag != "rss":
        raise ValueError(f"feed root is {root.tag!r}, expected 'rss'")
    return text


def fetch_feed():
    # Retry on truncated reads (2026-10-29: the TWiT feed twice
    # stalled at ~196KB via urllib while curl fetched all 232KB, so
    # the proxy occasionally truncates a response mid-body). A feed
    # GET is idempotent, so retrying is safe.
    import http.client as _hc
    last = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(FEED_URL, headers={"User-Agent": "podcast-ad-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", errors="replace")
            return _validate_feed_xml(body)
        except ValueError as e:
            last = e
            log(f"  fetch_feed attempt {attempt + 1} invalid body ({e}); retrying")
            time.sleep(10)
        except (_hc.IncompleteRead, urllib.error.URLError) as e:
            last = e
            log(f"  fetch_feed attempt {attempt + 1} failed ({e}); retrying")
            time.sleep(10)
    # Last resort (2026-10-29): the proxy sometimes stalls urllib at
    # exactly the same byte count on every attempt (196095/232421 for
    # the TWiT feed, 08:05-08:10), while curl fetches it cleanly in
    # the same window — different read pattern, no stall. A feed GET
    # is idempotent, so falling back is safe.
    import subprocess as _sp
    try:
        out = _sp.run(["curl", "-sS", "--max-time", "60", FEED_URL],
                      capture_output=True, timeout=90)
        if out.returncode == 0 and out.stdout:
            try:
                return _validate_feed_xml(
                    out.stdout.decode("utf-8", errors="replace"))
            except ValueError as e:
                log(f"  fetch_feed curl fallback returned an invalid "
                    f"body ({e}); failing loudly")
                raise
        log(f"  fetch_feed curl fallback failed (rc={out.returncode})")
    except ValueError:
        raise
    except Exception as e:
        log(f"  fetch_feed curl fallback error: {e}")
    raise last


def _parse_chapter_start(s):
    """Parse a chapter start like '10:18' or '1:02:03' to seconds."""
    parts = s.strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return None


def _parse_rss_chapters(body):
    """Inline RSS chapter markers (<psc:chapter start title>).
    Returns [(start_sec, title)]."""
    out = []
    for cm in re.finditer(r'<psc:chapter\b[^>]*>', body):
        tag = cm.group(0)
        sm = re.search(r'start="([^"]+)"', tag)
        tm = re.search(r'title="([^"]+)"', tag)
        if not sm or not tm:
            continue
        sec = _parse_chapter_start(sm.group(1))
        if sec is None:
            continue
        out.append((sec, unescape(tm.group(1)).strip()))
    return out


def parse_items(xml):
    try:
        import transcript_diff
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import transcript_diff
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        body = m.group(1)
        def tag(name):
            mm = re.search(rf"<{name}[^>]*>(.*?)</{name}>", body, re.S)
            if not mm:
                return ""
            val = unescape(mm.group(1)).strip()
            # Strip CDATA wrapper (html.unescape doesn't handle it)
            if val.startswith("<![CDATA[") and val.endswith("]]>"):
                val = val[9:-3].strip()
            return val
        enc = re.search(r'<enclosure[^>]*url="([^"]+)"', body)
        items.append({
            "guid": tag("guid") or (enc.group(1) if enc else ""),
            "title": tag("title"),
            "pubDate": tag("pubDate"),
            "description": tag("description"),
            "audio_url": enc.group(1) if enc else "",
            "rss_chapters": _parse_rss_chapters(body),
            # Transcript rule (2026-11-11, owner): a feed carrying
            # podcast:transcript tags already has a second transcript
            # to compare against, so the dynamic-slot second download
            # is skipped for the whole podcast (see main() and
            # _run_dynamic_for_episode).
            "has_transcript": bool(re.search(r"<podcast:transcript\b", body)),
            # Publisher-transcript differential (2026-11-11, idea 6):
            # the raw transcript tags, for the corroboration stage in
            # process_episode. [] when the item carries no tag.
            "transcript_tags": transcript_diff.extract_transcript_tags(body),
        })
    return items


def extract_sponsors(description_html):
    """Sponsor brand names from the RSS description.

    Two sources: rel="sponsored" links (ground truth), and plain-text
    sponsor lists ("THANK YOU TO OUR SPONSORS! AG1 ... SHOPIFY ...").
    """
    brands = []
    for m in re.finditer(r'<a[^>]*rel="sponsored"[^>]*>(.*?)</a>', description_html, re.I | re.S):
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip().lower()
        text = re.sub(r"^https?://(www\.)?", "", text)
        text = text.split("/")[0]
        # NOTE: keep this TLD list in sync with the plain-text branch below
        # and with find_url_mentions' tlds. Missing a TLD is a silent miss:
        # SN 1101 (2026-10-22) — "canary.tools" kept its ".tools" suffix, so
        # the variant set never contained the spoken word "canary" and all
        # methods reported 0 mentions despite 16+ spoken mentions.
        root = re.sub(URL_TLD_RE, "", text)
        variants = {root, root.replace(".", " "), root.replace("-", " ")}
        # Bare domain stem: the host says "Canary", not "canary.tools" —
        # and Whisper tokenizes spoken URLs into separate words (and may
        # mangle the TLD: "canary.tools" -> "canary" ".tool"). Without the
        # stem, a dotted-domain sponsor matches nothing. Only for stems of
        # 4+ chars; the common-word filter below still drops generic stems.
        stem = root.split(".")[0] if "." in root else ""
        if len(stem) >= 4:
            variants.add(stem)
        # strip common marketing prefixes so the spoken brand is matched,
        # not just the URL token (2026-10-21: "joindeleteme" only matched
        # the URL near the end of the SN 1101 DeleteMe read, so no opening
        # cue was found and the whole read was missed)
        for prefix in ("drink", "get", "try", "take", "shop", "join"):
            if root.startswith(prefix) and len(root) - len(prefix) >= 4:
                variants.add(root[len(prefix):])
        # drop bare single common words that would false-positive
        variants = {v for v in variants if not (len(v.split()) == 1 and v in
                    {"material", "security", "one", "shield", "guard"})}
        if not variants:
            variants = {root.replace(".", " ")}
        brands.append({"label": text, "variants": {v for v in variants if v}})
    # Plain-text sponsor lists: "THANK YOU TO OUR SPONSORS! AG1 ... SHOPIFY ..."
    # Brand names come from the linked domains (ground truth); ALL-CAPS
    # names in the text supply extra match variants.
    text = re.sub(r"<[^>]+>", " ", description_html)
    text = unescape(text)
    sm = re.search(
        r"(thank you to our sponsors|thanks to our sponsors|series sponsor|our sponsors|sponsored by|sponsors?\s*:)"
        r"[:!\s]*(.{0,2000})", text, re.I | re.S)
    if sm:
        section = sm.group(2)
        # cut at the next all-caps header, social-links block, or long
        # dash/em-dash separator run (Lore/Libsyn "Sponsors:" format)
        section = re.split(r"\b(FOLLOW OUR|PATREON|MERCH|SUBSCRIBE)\b|[-—–]{3,}",
                           section, 1, flags=re.I)[0]
        # ALL-CAPS display names for variant enrichment
        display = []
        for cm in re.finditer(r"\b([A-Z][A-Z0-9&']{1,20}(?: [A-Z][A-Z0-9&']{1,20}){0,2})\b",
                             section):
            name = cm.group(1).strip()
            if name.upper() in {"THANK YOU TO OUR SPONSORS", "THANK YOU", "OUR SPONSORS",
                                "SPONSORS", "AD", "SERIES SPONSOR"} or len(name) < 2:
                continue
            display.append(name)
        # Title-Case display names (Libsyn "Series Sponsor: Warby Parker" format)
        for cm in re.finditer(r"\b([A-Z][a-z]{2,19}(?: [A-Z][a-z]{2,19}){0,2})\b",
                             section):
            name = cm.group(1).strip()
            if name.upper() in {"SERIES SPONSOR", "SPONSOR", "SPONSORS"} or len(name) < 4:
                continue
            display.append(name)
        # domains -> brand roots (scheme optional: Libsyn prints "WarbyParker.com/LORE")
        for um in re.finditer(r"(?<![\w:/.-])(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9.-]*\.[a-z]{2,})(?![a-z])", section, re.I):
            domain = um.group(1).lower()
            # skip social/video platforms
            if any(p in domain for p in ("instagram", "tiktok", "twitter", "x.com",
                                         "facebook", "youtube", "patreon", "pepperbox")):
                continue
            host = domain.split("/")[0]
            # Same extended TLD list as the sponsored-links branch above;
            # see the SN 1101 note there about silent misses.
            root = re.sub(URL_TLD_RE, "", host)
            # strip common marketing prefixes (drink, get, try, take, shop, join)
            root_stripped = re.sub(r"^(drink|get|try|take|shop|join)", "", root)
            if not root or len(root) < 2:
                continue
            # find a display name containing the root, for a nicer label
            label = root
            variants = {root}
            # Bare domain stem (same reason as the sponsored-links branch):
            # the host says the stem, and Whisper tokenizes spoken URLs into
            # separate words. Only for stems of 4+ chars.
            stem = root.split(".")[0] if "." in root else ""
            if len(stem) >= 4:
                variants.add(stem)
            # only use stripped variant if it's a plausible word (4+ chars)
            # e.g., "takeultra"->"ultra" yes, "shopify"->"ify" no
            if (root_stripped and root_stripped != root
                    and len(root_stripped) >= 4):
                variants.add(root_stripped)
            for d in display:
                dl = d.lower().replace(" ", "")
                # match if root in display, display in root, or share 4+ char substring
                match = (root in dl or dl in root.replace(" ", ""))
                if not match:
                    # check for shared distinctive substring
                    for i in range(len(root) - 3):
                        sub = root[i:i+4]
                        if len(sub) >= 4 and sub in dl and sub not in (
                                "http", "www."):
                            match = True
                            break
                if match:
                    label = d
                    variants.add(d.lower())
                    variants.add(dl)
                    # also add individual words from display name
                    for w in d.lower().split():
                        if len(w) >= 4:
                            variants.add(w)
                    break
            brands.append({"label": label, "variants": variants})
    # de-dupe preserving order
    seen, out = set(), []
    for b in brands:
        if b["label"] not in seen:
            seen.add(b["label"])
            out.append(b)
    return out


def _domain_stem(host):
    """Registrable-domain stem of a host: strip www, take the last two
    labels, drop the TLD ('www.hubermanlab.com' -> 'hubermanlab',
    'join.whoop.com' -> 'whoop', 'grc.com' -> 'grc')."""
    host = host.strip().lower()
    host = re.sub(r"^www\.", "", host)
    labels = [l for l in host.split(".") if l]
    if len(labels) >= 2:
        return labels[-2]
    return labels[0] if labels else ""


def extract_own_domains(description_html, channel_link=None, sponsors=None):
    """The show's own domains, as URL-brand stems, derived generically
    (imp5, 2026-11-07 A/B): the RSS channel <link> host plus the hosts
    of non-sponsored <a href> links in the description, MINUS sponsor
    stems. A sponsor's link may carry no rel="sponsored" in the markup
    (99pi's Claude link is rel="noopener noreferrer" yet Claude IS a
    description sponsor), so any stem matching a sponsor variant is
    excluded. Spoken URLs on the show's own domain ('grc.com' in the
    SN outro, 'theverge.com' on Vergecast, 'hubermanlab.com',
    '99pi.org') are content, not ad evidence. Returns e.g.
    {'grc', 'theverge', 'hubermanlab', '99pi'}."""
    hosts = []
    if channel_link:
        m = re.search(r"https?://([^/\"' >]+)", channel_link)
        if m:
            hosts.append(m.group(1))
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>',
                         description_html or ""):
        tag = m.group(0)
        if "sponsored" in tag.lower():
            continue
        hm = re.search(r"https?://([^/\"' >]+)", m.group(1))
        if hm:
            hosts.append(hm.group(1))
    stems = {_domain_stem(h) for h in hosts}
    stems.discard("")
    if sponsors:
        sponsor_variants = set()
        for sp in sponsors:
            sponsor_variants |= set(sp.get("variants", ()))
        stems = {s for s in stems
                 if s not in sponsor_variants and
                 not any((s in v or v in s) for v in sponsor_variants
                         if len(v) >= 4)}
    return stems


def parse_channel_link(xml):
    """The RSS channel-level <link> (the show's home page), for
    extract_own_domains. First <link> before the first <item>."""
    head = xml.split("<item>", 1)[0]
    m = re.search(r"<link[^>]*>(.*?)</link>", head, re.S)
    return unescape(m.group(1)).strip() if m else None


# How many rotated state backups save_state() keeps (M29, 2026-11-19).
STATE_BACKUP_KEEP = 5


@contextlib.contextmanager
def state_lock():
    """Exclusive per-podcast state lock (M28, 2026-11-19).

    Held across load-modify-save by writers that mutate state
    (publish_github, publish_feed). save_state() itself takes it
    around the write+replace, so two concurrent saves can no longer
    share one .tmp path and lose the loser's write. flock is
    per-open-file-description, so threads in one process contend
    exactly like separate processes. The lock file lives next to
    state.json and is never deleted (deleting it would break the
    mutual exclusion). Re-entrant on the same thread (a threading
    RLock + depth counter), so save_state() inside an outer
    state_lock() cannot deadlock on its own flock."""
    if STATE_PATH is None:
        raise TypeError("STATE_PATH is None; call init_podcast() first")
    _depth = getattr(_state_lock_local, "depth", 0)
    if _depth:
        _state_lock_local.depth = _depth + 1
        try:
            yield
        finally:
            _state_lock_local.depth = _depth
        return
    _state_lock_rlock.acquire()
    lock_path = STATE_PATH + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _state_lock_local.depth = 1
        try:
            yield
        finally:
            _state_lock_local.depth = 0
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            _state_lock_rlock.release()


_state_lock_rlock = threading.RLock()
_state_lock_local = threading.local()


def _newest_state_backup():
    """Newest parseable state.json.bak-* backup, or None (M29)."""
    if STATE_PATH is None:
        return None
    cands = sorted(
        (p for p in glob.glob(STATE_PATH + ".bak-*")
         if os.path.isfile(p)),
        key=lambda p: os.path.getmtime(p), reverse=True)
    for p in cands:
        try:
            with open(p) as f:
                json.load(f)
            return p
        except Exception:
            continue
    return None


# T3-N9: the state this process loaded (snapshot = the exact dict
# object load_state() returned; baseline = its JSON-normalised copy).
# save_state() merges the caller's diff against the baseline onto the
# fresh disk state. Both start as None (no load yet in this process).
_state_baseline = None
_state_snapshot = None


def _one_level_diff(old, new):
    """(set, del) between two flat dicts: keys whose value changed or
    is new, and keys that disappeared. Values are compared by ==."""
    sets = {k: v for k, v in new.items()
            if k not in old or old[k] != v}
    dels = [k for k in old if k not in new]
    return sets, dels


def _state_diff(old, new):
    """Diff between the loaded baseline and the caller's state (T3-N9).

    Returns {"set", "del", "sub"}: whole-value top-level sets/deletes,
    and per-subkey diffs for dict-valued top-level keys. "processed"
    gets one more level (per-episode add/drop, per-entry-key set/del)
    so a marking run and a publish run touching the same episode
    keep each other's fields; "process_crashes" and "dynamic_ads"
    get per-subkey diffs the same way. Entry/subkey values are
    compared by ==."""
    diff = {"set": {}, "del": [], "sub": {}}
    for k, v in new.items():
        if k not in old:
            diff["set"][k] = v
        elif k == "processed":
            old_p = old[k] or {}
            new_p = v or {}
            entry = {}
            for g, e in new_p.items():
                if g not in old_p:
                    entry[g] = {"add": e}
                else:
                    s, d = _one_level_diff(old_p[g], e)
                    if s or d:
                        entry[g] = {"set": s, "del": d}
            drops = [g for g in old_p if g not in new_p]
            if entry or drops:
                diff["sub"][k] = {"entry": entry, "drop": drops}
        elif isinstance(v, dict) and isinstance(old[k], dict):
            s, d = _one_level_diff(old[k], v)
            if s or d:
                diff["sub"][k] = {"set": s, "del": d}
        elif old[k] != v:
            diff["set"][k] = v
    for k in old:
        if k not in new:
            diff["del"].append(k)
    return diff


def _state_apply_diff(fresh, diff):
    """Apply a _state_diff onto a freshly loaded state (T3-N9).

    Everything the diff does not touch is taken from `fresh` as-is,
    so a concurrent writer's changes survive the save."""
    out = dict(fresh)
    out.update(diff["set"])
    for k in diff["del"]:
        out.pop(k, None)
    for k, sd in diff["sub"].items():
        if "entry" in sd:  # "processed": per-episode merge
            proc = dict(out.get(k, {}) or {})
            for g, ch in sd["entry"].items():
                if "add" in ch:
                    proc[g] = ch["add"]
                else:
                    e = dict(proc.get(g, {}))
                    e.update(ch["set"])
                    for dk in ch["del"]:
                        e.pop(dk, None)
                    proc[g] = e
            for g in sd["drop"]:
                proc.pop(g, None)
            out[k] = proc
        else:  # other dict-valued top-level key: per-subkey merge
            cur = dict(out.get(k, {}) or {})
            cur.update(sd["set"])
            for dk in sd["del"]:
                cur.pop(dk, None)
            out[k] = cur
    return out


def _load_state_raw():
    """Read the current disk state WITHOUT touching the T3-N9
    baseline/snapshot (used by the final-save rebuild in
    process_episode() / _retry_dynamic_only()). Falls back to the
    newest backup like load_state(); returns a fresh empty state
    when no state file exists. Raises ValueError when nothing
    parses."""
    if STATE_PATH is None:
        raise TypeError("STATE_PATH is None; call init_podcast() first")
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            bak = _newest_state_backup()
            if bak is not None:
                with open(bak) as f:
                    return json.load(f)
            raise ValueError(
                f"state.json unreadable ({e}) and no parseable "
                f"state.json.bak-* backup exists") from e
    return {"processed": {}, "base_url": ""}


def load_state():
    """Load state.json, falling back to the newest backup (M29).

    A corrupt/truncated state file used to raise JSONDecodeError out
    of every entry point and brick the whole podcast run. Now the
    newest state.json.bak-* backup is tried (and logged); only when
    no backup parses does the error propagate, naming the backups
    that were tried.

    T3-N9: the returned dict is recorded as this process's loaded
    snapshot (_state_snapshot) with a JSON-normalised copy as the
    baseline (_state_baseline). save_state() merges the caller's
    diff against that baseline onto the fresh disk state instead of
    overwriting the whole file, so a concurrent writer's changes
    survive. One live snapshot per process: a second load_state()
    resets both; a state dict that was not produced by load_state()
    in this process is saved whole-file, exactly as before."""
    global _state_baseline, _state_snapshot
    if STATE_PATH is None:
        raise TypeError("STATE_PATH is None; call init_podcast() first")
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            bak = _newest_state_backup()
            if bak is not None:
                log(f"  WARNING: state.json unreadable ({e}); falling "
                    f"back to backup {bak}")
                with open(bak) as f:
                    state = json.load(f)
            else:
                raise ValueError(
                    f"state.json unreadable ({e}) and no parseable "
                    f"state.json.bak-* backup exists") from e
    else:
        state = {"processed": {}, "base_url": ""}
    _state_snapshot = state
    _state_baseline = json.loads(json.dumps(state))
    return state


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _fsync_file(path):
    """Flush a file to disk before an atomic replace (same mechanism as
    _fsync_dir; kept separate so the call sites read correctly)."""
    _fsync_dir(path)


def _tmp_path(path):
    """Temp sibling for an atomic write, keeping the extension: ffmpeg
    infers the output format from it, so <out>.tmp.<pid> (no .mp3)
    would make ffmpeg refuse the mux. E.g. foo.mp3 -> foo.tmp.123.mp3."""
    root, ext = os.path.splitext(path)
    return f"{root}.tmp.{os.getpid()}{ext}"


def save_state(state):
    """Atomic, locked state write with backup rotation (M28/M29).

    Unique temp name per writer (pid + thread + random) so two
    concurrent writers can no longer share one .tmp path; the file
    is fsynced before os.replace and the directory fsynced after,
    so a crash cannot lose the just-written state. The previous
    state file is rotated to state.json.bak-<timestamp> first
    (newest STATE_BACKUP_KEEP kept). The write+replace holds the
    per-podcast state lock.

    T3-N4: the backup stamp carries microseconds, so rapid saves no
    longer collide on one 1-second stamp and the rotation really
    keeps 5 backups. T3-N5: the current state is parsed before it
    is archived -- a corrupt current state is never written over a
    good backup. T3-N9: when `state` is this process's loaded
    snapshot, only the caller's diff against its load-time baseline
    is applied onto the freshly read disk state, so a concurrent
    writer's changes (e.g. a publish record) survive this save; a
    state that was not loaded here, or a disk state that does not
    parse, falls back to the whole-file write, exactly as before."""
    global _state_baseline
    if STATE_PATH is None:
        raise TypeError("STATE_PATH is None; call init_podcast() first")
    with state_lock():
        # T3-N9: read the current disk state once, under the lock --
        # the merge base, and (T3-N5) the proof it parses.
        fresh = None
        fresh_from_disk = False
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH) as f:
                    fresh = json.load(f)
                fresh_from_disk = True
            except (OSError, ValueError):
                # Disk state is corrupt: do not merge onto a stale
                # backup (it could drop keys the caller saw) and do
                # not archive the corrupt file (T3-N5). The
                # whole-file write below replaces it.
                log("  WARNING: current state.json unreadable; not "
                    "archiving it as a backup and not merging onto "
                    "a backup (T3-N5/T3-N9)")
        to_write = state
        merged = False
        if (fresh_from_disk and state is _state_snapshot
                and _state_baseline is not None):
            to_write = _state_apply_diff(
                fresh, _state_diff(_state_baseline, state))
            merged = True
        if fresh_from_disk:
            stamp = time.strftime("%Y%m%d-%H%M%S") + ".%06d" % (
                int(time.time() * 1000000) % 1000000)
            bak = f"{STATE_PATH}.bak-{stamp}"
            try:
                import shutil
                shutil.copyfile(STATE_PATH, bak)
                olds = sorted(
                    (p for p in glob.glob(STATE_PATH + ".bak-*")
                     if os.path.isfile(p)),
                    key=lambda p: os.path.getmtime(p), reverse=True)
                for p in olds[STATE_BACKUP_KEEP:]:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            except OSError as e:
                log(f"  WARNING: state backup rotation failed ({e}); "
                    "continuing without a backup")
        tmp = (f"{STATE_PATH}.tmp.{os.getpid()}."
               f"{threading.get_ident()}.{random.randint(0, 1 << 30)}")
        with open(tmp, "w") as f:
            json.dump(to_write, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
        _fsync_dir(os.path.dirname(STATE_PATH) or ".")
        if state is _state_snapshot:
            # The baseline advances to what is now on disk. After a
            # merged save the caller's dict is refreshed in place to
            # the merged content: without this, the next save would
            # diff the caller's stale values against the new baseline
            # and re-apply them as changes, clobbering a concurrent
            # writer's newer values. (A whole-file save needs no
            # refresh -- the caller already holds what was written.)
            _state_baseline = json.loads(json.dumps(to_write))
            if merged:
                state.clear()
                state.update(json.loads(json.dumps(to_write)))


def _download_sidecar_path(dest):
    return dest + ".download.json"


def _write_download_sidecar(dest, content_length_verified):
    """Record the identity of a completed download (M1): bytes + SHA-256
    + ffprobe duration. Written only after a download completed — never
    for a legacy file on trust alone (T1-N7: ffprobe duration cannot
    prove a file complete, so attesting a legacy file would bless a
    truncation forever). Never compared against a live HEAD/length
    (entry-52 rotation lesson) — only against this sidecar. A sidecar
    write failure never fails the download."""
    try:
        duration = None
        try:
            duration = probe_duration(dest)
        except Exception:
            pass
        _atomic_json_dump(_download_sidecar_path(dest), {
            "bytes": os.path.getsize(dest),
            "sha256": sha256_file(dest),
            "duration": duration,
            "content_length_verified": bool(content_length_verified)})
    except Exception as exc:
        log(f"  download sidecar write failed ({exc!r}); continuing")


def _verify_download_sidecar(dest):
    """True when dest matches its sidecar (size + SHA-256)."""
    try:
        with open(_download_sidecar_path(dest)) as f:
            sc = json.load(f)
        return (os.path.getsize(dest) == sc.get("bytes") and
                sha256_file(dest) == sc.get("sha256"))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _curl_download(url, dest):
    """One curl attempt (fetch_feed's fallback pattern). Raises on
    failure; the caller decides what to do with it."""
    tmp = f"{dest}.curltmp.{os.getpid()}"
    out = subprocess.run(
        ["curl", "-fsSL", "--max-time", "600", "-o", tmp, url],
        capture_output=True, timeout=660)
    if out.returncode != 0 or not os.path.exists(tmp) or \
            os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise IOError(f"curl fallback failed (rc={out.returncode})")
    os.replace(tmp, dest)


def _is_retryable_download_error(e):
    """Permanent failures fail fast (m21): 4xx other than 408/429.
    Everything else (5xx, 408/429, timeouts, truncated reads,
    content-length mismatch, network errors) is retryable."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (408, 429) or e.code >= 500
    return True


def download(url, dest, retries=6):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        # (M1) A present file is trusted ONLY against its sidecar —
        # never against a live HEAD/length (Megaphone rotates).
        if os.path.exists(_download_sidecar_path(dest)):
            if _verify_download_sidecar(dest):
                log(f"already downloaded: {dest} (verified against sidecar)")
                return
            log(f"  {dest} differs from its download sidecar; "
                "re-downloading")
            for p in (dest, _download_sidecar_path(dest)):
                try:
                    os.remove(p)
                except OSError:
                    pass
        else:
            # Legacy file with no sidecar. It is NEVER trusted (T1-N7):
            # ffprobe reports the header duration even for a file
            # truncated to 2% of its bytes, so a parseable legacy file
            # may still be truncated — and writing a sidecar for it
            # would bless that truncation forever. Re-download once;
            # the fresh download writes its own sidecar and is pinned
            # from then on. Never verified against a live HEAD/length
            # (entry-52 rotation lesson).
            try:
                legacy_dur = probe_duration(dest)
            except Exception:
                legacy_dur = 0.0
            if legacy_dur <= 0:
                log(f"  {dest} present but unparseable/zero-duration; "
                    "re-downloading")
                try:
                    os.remove(dest)
                except OSError:
                    pass
            else:
                log(f"  {dest} present with no download sidecar "
                    "(legacy); ffprobe duration cannot prove it is "
                    "complete, re-downloading once")
                keep = dest + ".legacy-keep"
                try:
                    os.replace(dest, keep)
                except OSError as exc:
                    # Cannot preserve the file: fall back to deleting
                    # it and re-downloading (the unparseable shape
                    # above). A verified copy is the only safe
                    # outcome; the download loop already accepts
                    # this shape.
                    log(f"  could not set aside legacy file ({exc!r}); "
                        "deleting and re-downloading")
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                    keep = None
                if keep is not None:
                    try:
                        download(url, dest, retries=retries)
                    except Exception:
                        # Fresh download failed: restore the legacy
                        # file intact and re-raise. A failed download
                        # never destroys the only copy.
                        try:
                            os.replace(keep, dest)
                        except OSError:
                            pass
                        log(f"  fresh download failed; legacy file "
                            f"restored: {dest}")
                        raise
                    try:
                        os.remove(keep)
                    except OSError as exc:
                        log(f"  could not remove set-aside legacy file "
                            f"{keep} ({exc!r}); leaving it in place")
                    return
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            if attempt > 1:
                log(f"downloading {url} (attempt {attempt}/{retries})")
            else:
                log(f"downloading {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "podcast-ad-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                written = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                # Content-Length check (2026-11-19): a short read used
                # to look like success (truncated B for Unsubscribe
                # 289 was caught only by the duration guard). Generic
                # check, no hardcoded sizes: compare bytes written to
                # the advertised length; on a mismatch raise inside
                # the try so the existing retry/cleanup engages. A
                # missing or unparseable header (e.g. chunked
                # encoding) skips the check — fail open.
                clen = None
                try:
                    clen = r.getheader("Content-Length")
                except AttributeError:
                    clen = r.headers.get("Content-Length") \
                        if hasattr(r, "headers") else None
                expected = None
                if clen is not None:
                    try:
                        expected = int(clen)
                    except (TypeError, ValueError):
                        expected = None
                if expected is not None and written != expected:
                    raise IOError(
                        f"download truncated: {url}: got {written} "
                        f"bytes, expected {expected} (Content-Length)")
                cl_verified = expected is not None
            log(f"saved {os.path.getsize(dest)} bytes")
            _write_download_sidecar(dest, cl_verified)
            return
        except Exception as e:
            last_err = e
            # drop the partial file so a retry starts clean
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            log(f"download attempt {attempt} failed: {type(e).__name__}: {e}")
            if not _is_retryable_download_error(e):
                raise
            if attempt < retries:
                wait = min(20 * attempt, 60)
                log(f"retrying download in {wait}s...")
                time.sleep(wait)
    # Retries exhausted on a retryable error: one curl fallback
    # (fetch_feed's pattern for the proxy stalling urllib).
    try:
        log("  download urllib retries exhausted; trying curl fallback")
        _curl_download(url, dest)
        _write_download_sidecar(dest, False)
        log(f"saved {os.path.getsize(dest)} bytes (via curl)")
        return
    except Exception as exc:
        log(f"  curl fallback failed ({exc!r})")
    raise last_err


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True, timeout=120)
    return float(out.stdout.strip())


_MODEL = None


def load_model():
    """Load the base Whisper model once per process (m4): A and B
    transcription in one episode share it instead of loading twice."""
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel("base", device="cpu", compute_type="int8",
                              local_files_only=True)
    return _MODEL


def transcribe_file(model, wav_path, **kwargs):
    """Transcribe via a numpy array.

    faster-whisper 1.2.1 passes metadata_errors to av.open(), which the
    installed PyAV 19 no longer accepts — feeding raw audio avoids av.
    """
    import numpy as np
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", wav_path, "-ar", "16000", "-ac", "1",
         "-f", "f32le", "-acodec", "pcm_f32le", "-"],
        capture_output=True, check=True, timeout=300).stdout
    audio = np.frombuffer(out, dtype=np.float32)
    return model.transcribe(audio, **kwargs)


def _atomic_json_dump(path, payload, indent=None):
    """Write JSON atomically (temp + replace) so a kill mid-write never
    leaves a truncated file for the next run to choke on (m19)."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=indent)
    os.replace(tmp, path)


def _drop_transcript_cache(work_dir):
    """Delete every cached chunk + transcript.json in a work dir.

    (N1) The drop is verified, the m1 pattern from _b_cache_check:
    a surviving file raises RuntimeError instead of letting the
    caller return the stale cache and re-bless it with a fresh
    a_identity.json."""
    for f in glob.glob(os.path.join(work_dir, "chunk_*.json")):
        try:
            os.remove(f)
        except OSError:
            pass
    try:
        os.remove(os.path.join(work_dir, "transcript.json"))
    except OSError:
        pass
    survivors = glob.glob(os.path.join(work_dir, "chunk_*.json"))
    if os.path.exists(os.path.join(work_dir, "transcript.json")):
        survivors.append(os.path.join(work_dir, "transcript.json"))
    if survivors:
        log(f"  ERROR: stale A chunks could not be removed: {survivors}; "
            "refusing to reuse the cache")
        raise RuntimeError(
            f"_drop_transcript_cache: could not remove stale A cache in "
            f"{work_dir}: {survivors}")


def _prune_out_of_range_chunks(work_dir, duration):
    """Delete chunk_*.json files whose start is >= duration (M3):
    chunks from an older, longer cut that glob readers would
    otherwise include."""
    for f in glob.glob(os.path.join(work_dir, "chunk_*.json")):
        try:
            idx = int(os.path.basename(f)[6:9])
        except ValueError:
            continue
        if idx * TRANSCRIBE_CHUNK >= duration:
            try:
                os.remove(f)
            except OSError:
                pass


def _transcript_cache_complete(work_dir, duration):
    """True when every in-range chunk file exists with a matching
    transcribe config (M4 fast-path precondition)."""
    n_chunks = int(duration // TRANSCRIBE_CHUNK) + 1
    for i in range(n_chunks):
        if i * TRANSCRIBE_CHUNK >= duration:
            break
        p = os.path.join(work_dir, f"chunk_{i:03d}.json")
        if not os.path.exists(p):
            return False
        try:
            with open(p) as f:
                data = json.load(f)
            if not _transcribe_config_matches(
                    data.get("transcribe_config", TRANSCRIBE_CONFIG)):
                return False
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            return False
    return True


def _write_audio_identity(path, sha, size, duration):
    try:
        _atomic_json_dump(path, {"sha256": sha, "size": size,
                                 "duration": duration})
    except OSError:
        pass


def _stored_config_has_revision(stored):
    """True when a stored transcribe config records a model revision
    (N6). Pre-patch chunks carry none; their weights predate tracking
    and are accepted by design."""
    return isinstance(stored, dict) and "model_revision" in stored


def _has_unstamped_chunk(work_dir, duration):
    """True when any in-range cached chunk carries no model revision
    (N6): accepted, but the exposure is worth one warning per run."""
    n_chunks = int(duration // TRANSCRIBE_CHUNK) + 1
    for i in range(n_chunks):
        if i * TRANSCRIBE_CHUNK >= duration:
            break
        try:
            with open(os.path.join(work_dir, f"chunk_{i:03d}.json")) as f:
                data = json.load(f)
            if not _stored_config_has_revision(
                    data.get("transcribe_config")):
                return True
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            continue
    return False


def transcribe_chunked(mp3_path, work_dir):
    """Transcribe in short chunks with word timestamps; resumable.

    Cache identity (2026-11-19 fix round, issue (a)): the chunk cache
    belongs to the exact audio file it was transcribed from.
    `a_identity.json` records the A mp3's SHA-256 + size; a mismatch
    (or a missing identity file, i.e. a legacy cache) drops the chunks
    so a rotated A is never transcribed against a stale transcript.
    Stale out-of-range chunks (start >= duration) are pruned (M3).
    A complete cached transcript is returned without probing the mp3
    or loading the model (M4). A zero/negative probe duration raises
    instead of writing an empty transcript (M2)."""
    os.makedirs(work_dir, exist_ok=True)
    # Sweep orphan chunk wavs left by a killed earlier run (m6).
    for _f in glob.glob(os.path.join(work_dir, "chunk_*.wav")) + \
            glob.glob(os.path.join(work_dir, "tmp*.wav")):
        try:
            os.remove(_f)
        except OSError:
            pass
    # (a) A audio identity — SHA-256 + size, no probe, no model.
    audio_sha, audio_size = None, None
    try:
        audio_sha = sha256_file(mp3_path)
        audio_size = os.path.getsize(mp3_path)
    except OSError:
        log("  A audio unreadable for identity check; "
            "using cached transcript if complete")
    identity_path = os.path.join(work_dir, "a_identity.json")
    if audio_sha is not None:
        drop = False
        if os.path.exists(identity_path):
            try:
                ident = json.load(open(identity_path))
                if ident.get("sha256") != audio_sha or \
                        ident.get("size") != audio_size:
                    log("  A audio differs from the cached transcript; "
                        "dropping stale A chunks")
                    drop = True
            except (OSError, ValueError):
                log("  A identity file unreadable; dropping stale A chunks")
                drop = True
        else:
            # Legacy cache with no identity record: no evidence the
            # chunks belong to this audio (entry-51 rule for the B).
            if glob.glob(os.path.join(work_dir, "chunk_*.json")) or \
                    os.path.exists(os.path.join(work_dir, "transcript.json")):
                log("  no A transcript identity; dropping any stale A chunks")
                drop = True
        if drop:
            _drop_transcript_cache(work_dir)
    # (M4) Complete-cache fast path: no probe, no model.
    full = os.path.join(work_dir, "transcript.json")
    if os.path.exists(full):
        try:
            with open(full) as f:
                tj = json.load(f)
            dur = float(tj["duration"])
            cached_words = [(w[0], w[1], w[2]) for w in tj["words"]]
            if dur > 0 and cached_words and \
                    _transcript_cache_complete(work_dir, dur):
                _prune_out_of_range_chunks(work_dir, dur)
                # (N6) Pre-patch chunks carry no model revision:
                # accepted by design, but flag the exposure once.
                if _has_unstamped_chunk(work_dir, dur):
                    log("  WARNING: cached chunk(s) carry no model "
                        "revision (pre-patch chunks); accepted, model "
                        "provenance unknown")
                if audio_sha is not None:
                    _write_audio_identity(identity_path, audio_sha,
                                          audio_size, dur)
                return cached_words, dur
        except (OSError, ValueError, KeyError, AttributeError, TypeError,
                IndexError):
            pass  # fall through to probe + transcribe
    duration = probe_duration(mp3_path)
    if duration <= 0:
        raise ValueError(
            f"transcribe_chunked: probe_duration returned {duration} "
            f"for {mp3_path}; refusing to write an empty transcript")
    _prune_out_of_range_chunks(work_dir, duration)
    model = None
    unstamped_warned = False
    words = []  # (start, end, text)
    n_chunks = int(duration // TRANSCRIBE_CHUNK) + 1
    for i in range(n_chunks):
        start = i * TRANSCRIBE_CHUNK
        if start >= duration:
            break
        chunk_json = os.path.join(work_dir, f"chunk_{i:03d}.json")
        if os.path.exists(chunk_json):
            try:
                with open(chunk_json) as f:
                    data = json.load(f)
                if _transcribe_config_matches(
                        data.get("transcribe_config", TRANSCRIBE_CONFIG)):
                    # (N6) Pre-patch chunks carry no model revision:
                    # accepted by design, but flag the exposure once.
                    if not unstamped_warned and \
                            not _stored_config_has_revision(
                                data.get("transcribe_config")):
                        unstamped_warned = True
                        log("  WARNING: cached chunk(s) carry no model "
                            "revision (pre-patch chunks); accepted, model "
                            "provenance unknown")
                    words.extend([(w[0], w[1], w[2]) for w in data["words"]])
                    continue
            except (OSError, ValueError, KeyError, AttributeError,
                    TypeError, IndexError):
                log(f"  chunk {i}: cached chunk unreadable; re-transcribing")
            else:
                log(f"  chunk {i}: transcribe config changed; re-transcribing")
        if model is None:
            model = load_model()
        chunk_wav = os.path.join(work_dir, f"chunk_{i:03d}.wav")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", str(start),
                 "-t", str(TRANSCRIBE_CHUNK), "-i", mp3_path,
                 "-ar", "16000", "-ac", "1", chunk_wav],
                check=True, timeout=300)
            segments, _ = transcribe_file(model, chunk_wav,
                                          word_timestamps=True)
            cwords = []
            for seg in segments:
                for w in (seg.words or []):
                    cwords.append((w.start + start, w.end + start, w.word))
            _atomic_json_dump(chunk_json,
                              {"words": cwords,
                               "transcribe_config":
                                   _current_transcribe_config()})
        finally:
            try:
                os.remove(chunk_wav)
            except OSError:
                pass
        words.extend(cwords)
        log(f"transcribed chunk {i + 1}/{n_chunks}")
    # m13 (2026-11-19): sort + dedupe words before writing, so a
    # non-monotonic or duplicated transcript cannot silently change
    # detection (detect_ad_blocks also sorts on entry).
    words = _sorted_words(words)
    _atomic_json_dump(full, {"words": words, "duration": duration})
    if audio_sha is not None:
        _write_audio_identity(identity_path, audio_sha, audio_size, duration)
    return words, duration


def find_silences(mp3_path, work_dir, min_dur=1.5):
    """Return list of (silence_start, silence_end) via ffmpeg silencedetect."""
    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", mp3_path, "-af",
         f"silencedetect=noise=-35dB:d={min_dur}", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1200).stderr
    silences = []
    starts = re.findall(r"silence_start: ([\d.]+)", out)
    ends = re.findall(r"silence_end: ([\d.]+)", out)
    for s, e in zip(starts, ends):
        silences.append((float(s), float(e)))
    return silences


def norm_words(words):
    """Whitespace-normalized lowercase words for matching (indices align)."""
    return [re.sub(r"\s+", " ", w[2]).strip().lower() for w in words]


def _sorted_words(words):
    """Words sorted by start time, exact duplicates removed (m13).

    Detection assumes non-decreasing word starts (_word_votes bisects
    on starts; the cluster window and the heat-map duration read
    words[-1]). A stale/mixed chunk cache can hand detect_ad_blocks a
    reordered or duplicated list, which silently truncates detection
    at words[-1][0] and changes votes. Sorting here (and in
    transcribe_chunked, before the transcript is written) makes
    detection order-independent. Exact (start, end, text) triples are
    duplicates; genuine repeated words differ in start time.
    """
    return sorted(set(words), key=lambda w: (w[0], w[1], w[2]))


def _boundary_match(v, w):
    """True if short variant v occurs in token w at a word boundary.

    A short brand variant (len<6) may match inside a transcript word
    only at a word boundary, not mid-word: 'aura' must not match
    inside 'restaurant', 'stop' must not match inside 'christopher'
    or 'stopping'. Before: start of token or a non-alphanumeric char.
    After: end of token, a non-alphanumeric char, a plural/possessive
    suffix ('s', 'es', "'s"), or a single-letter remainder — the token
    is the variant plus one letter, a spelling/transcription variant
    of the brand ('noble' for NOBL; the short-variant analogue of the
    len>=6 fuzzy path's >=0.85 ratio). A multi-letter remainder
    ('sleeping', 'seeding') still fails.
    """
    start = 0
    while True:
        i = w.find(v, start)
        if i == -1:
            return False
        if i == 0 or (not w[i - 1].isalnum()):
            before_ok = True
        else:
            before_ok = False
        after = w[i + len(v):]
        if after == "" or (not after[0].isalnum()):
            after_ok = True
        elif after.startswith("'s") and (len(after) == 2
                                         or not after[2].isalnum()):
            after_ok = True
        elif after.startswith("es") and (len(after) == 2
                                         or not after[2].isalnum()):
            after_ok = True
        elif after.startswith("s") and (len(after) == 1
                                        or not after[1].isalnum()):
            after_ok = True
        elif len(after.rstrip(".,!?;:")) == 1 and \
                after.rstrip(".,!?;:").isalnum():
            after_ok = True
        else:
            after_ok = False
        if before_ok and after_ok:
            return True
        start = i + 1

def _prefix_guard_ok(v, w):
    """A token that merely STARTS with the brand plus a non-plural suffix
    is a different word, not a mangling: 'factory' is not Factor (0.92),
    'progressively' is not Progressive. Trailing punctuation does not
    count as a suffix."""
    if not w.startswith(v):
        return True
    suf = w[len(v):].strip(".,!?;:")
    return suf in ("s", "es", "'s", "")


def _stem_plural(t):
    """Strip a trailing plural suffix for symmetric comparison (T2-N8):
    "greens" -> "green", "boxes" -> "box", "1s" -> "1". Only a trailing
    "es"/"s" is stripped — never a substring — so "minty" is untouched
    and the M16 "minty mobile" guard still holds. Length floors keep
    1-2 letter tokens ("us", "s", "es") from stemming to a stub."""
    if t.endswith("es") and len(t) > 2:
        return t[:-2]
    if t.endswith("s") and len(t) > 1:
        return t[:-1]
    return t


def find_mentions(nw, variants):
    """Word indices mentioning a brand.

    Handles 'bit warden' -> 'bitwarden' (split words) and whisper
    mis-transcriptions like 'threadlocker' -> 'threatlocker' (fuzzy).
    Short variants match only at a word boundary (_boundary_match).
    """
    from difflib import SequenceMatcher
    hits = []
    L = len(nw)
    for v in variants:
        toks = v.split()
        if len(toks) == 1 and len(v) >= 6:
            for i in range(L):
                w = nw[i]
                if w == v or w in (v + "s", v + "es", v + "'s"):
                    hits.append(i)
                elif SequenceMatcher(None, v, w).ratio() >= 0.85:
                    if not _prefix_guard_ok(v, w):
                        continue
                    hits.append(i)
            for i in range(L - 1):
                # Split compounds ('bit' + 'warden' -> 'bitwarden'):
                # only when the variant genuinely SPANS the boundary.
                # Without the containment guard, any word before
                # 'factory'/'factories' counted as 'factor' (the second
                # word alone contains the variant) — 287's Factor false
                # block came from exactly that leak.
                if v in (nw[i] + nw[i + 1]) and \
                        v not in nw[i] and v not in nw[i + 1]:
                    hits.append(i)
            continue
        if len(toks) == 1:
            # Short single-token variant: word-boundary match only.
            # A bare substring match leaks: 'aura' inside 'restaurant',
            # 'stop' inside 'christopher'/'stopping' (2026-11-12).
            v = toks[0]
            for i in range(L):
                w = nw[i]
                if _boundary_match(v, w):
                    hits.append(i)
            continue
        n = len(toks)
        for i in range(L - n + 1):
            # Exact token equality (+ plural/possessive suffix only):
            # a substring test per word leaks ("ag" in "again", "1" in
            # "21", "mint" in "minty" — M16). _boundary_match is NOT
            # used here: its single-letter-remainder tolerance would
            # still match "minty mobile" for "mint mobile".
            # T2-N8: the suffix test is one-sided (plural SPEECH
            # matches a singular variant, not vice versa), so a
            # plural variant token never matches singular speech
            # ("athletic greens" vs "athletic green"). Stemming both
            # sides (trailing s/es only) restores symmetry. This is
            # not fuzzy matching: "mint" vs "minty" still fails
            # (no trailing s), so M16 stays fixed.
            if all(nw[i + j] == toks[j] or
                   nw[i + j] in (toks[j] + "s", toks[j] + "es",
                                 toks[j] + "'s") or
                   _stem_plural(nw[i + j]) == _stem_plural(toks[j])
                   for j in range(n)):
                hits.append(i)
    return sorted(set(hits))


def find_produced_spot(words, nw, hits, silences):
    """Fallback for produced spots with no host cue (e.g. Guardsquare).

    Pattern: brand mention + '.com' URL + 'thank ... support' sign-off
    clustered together. Returns (start, end) or None.
    """
    anchor = anchor_hit = None
    for h in hits:
        for i in range(h, min(len(nw), h + 200)):
            window = " ".join(nw[i:i + 10])
            if "thank" in window and "support" in window:
                anchor, anchor_hit = i, h
                break
        if anchor is not None:
            break
    if anchor is None:
        return None
    if not any(".com" in nw[i] for i in range(anchor_hit, min(len(nw), anchor + 12))):
        return None
    t_anchor = words[anchor][0]
    cluster_start_t = t_anchor
    for h in hits:
        t = words[h][0]
        if t_anchor - 60 <= t <= t_anchor and t < cluster_start_t:
            cluster_start_t = t
    seg_start = max(0.0, cluster_start_t - 8.0)
    for s_start, s_end in silences:
        if s_end <= cluster_start_t and s_end >= cluster_start_t - 30:
            seg_start = s_end  # closest silence before the spot
    # end: thanks sign-off (plus 'Back to Steve'-style handoff), then pause
    t = next((k for k in range(anchor, min(anchor + 10, len(nw))) if "thank" in nw[k]), anchor)
    seg_end = snap_forward(get_pauses(words, silences), thanks_end(words, nw, t))
    dur = seg_end - seg_start
    if not (15 <= dur <= 150):
        return None
    return seg_start, seg_end


def get_pauses(words, silences):
    """Pauses from word gaps (>=0.7s) union silencedetect silences."""
    pauses = [(s, e) for s, e in silences]
    for a, b in zip(words, words[1:]):
        if b[0] - a[1] >= 0.7:
            pauses.append((a[1], b[0]))
    pauses.sort()
    return pauses


def thanks_end(words, nw, t):
    """End time of a thanks sign-off: the thanks sentence plus a following
    sign-off sentence ('happy to support you right back', 'Back to Steve')."""
    end_idx = t
    for j in range(t, min(t + 15, len(words))):
        if nw[j][-1:] in ".!?":
            end_idx = j
            break
    nxt = end_idx + 1
    if nxt < len(words):
        sent = " ".join(nw[nxt:nxt + 12])
        if any(k in sent for k in ("support", "happy to", "love them",
                                   "back to steve", "back to leo")):
            for j in range(nxt, min(nxt + 15, len(words))):
                if nw[j][-1:] in ".!?":
                    end_idx = j
                    break
    return words[end_idx][1]


def snap_forward(pauses, end_t, max_gap=4.0):
    for s_start, s_end in pauses:
        if s_start >= end_t and s_start - end_t <= max_gap and (s_end - s_start) >= 0.6:
            return s_start + 0.1
    return end_t + 0.2


def find_opening(words, nw, silences, first):
    """Find where a host-read ad starts. Returns seg_start or None."""
    open_idx = None
    for i in range(first, max(-1, first - 200), -1):
        window = " ".join(nw[max(0, i - 8):i + 1])
        if any(cue in window for cue in OPEN_CUES):
            open_idx = i
            break
    if open_idx is None:
        return None
    # snap the cut to the pause before the ad opening (clean sentence break):
    # locate the cue phrase's first word, then take the latest pause before it
    cue_start = open_idx
    win_start = max(0, open_idx - 8)
    for j in range(win_start, open_idx + 1):
        # T2-N2: locate the cue phrase by exact match at j, not by
        # substring over a 7-word window — the substring test can land
        # up to ~6 words before the cue, which makes the pause loop
        # below skip the silence before the cue entirely (its
        # p_end <= cue_t test fails on the too-early cue_t).
        _hit = False
        for cue in OPEN_CUES:
            _k = len(cue.split())
            _w = [w.strip(".,!?;:'\"") for w in nw[j:j + _k]]
            if " ".join(_w) == cue:
                _hit = True
                break
        if _hit:
            cue_start = j
            break
    cue_t = words[cue_start][0]
    # Prefer the latest sentence-ending word before the cue: a clean
    # sentence break even when the pause there is short. The long-pause
    # rule below can otherwise snap back to a mid-sentence comma pause
    # (SN 1101 DeleteMe: it snapped to 2610, mid-teaser, instead of
    # "Take it away." at 2619).
    for w in reversed(words):
        if w[1] > cue_t:
            continue
        if w[1] < cue_t - 30:
            break
        if w[2].strip().endswith((".", "!", "?")):
            return w[1]
    for p_start, p_end in reversed(get_pauses(words, silences)):
        if p_end <= cue_t and p_end >= cue_t - 30 and (p_end - p_start) >= 0.6:
            # T2-N2: return the silence END (the speech-side boundary
            # for a start edge), not its start.
            return p_end
    return max(0.0, words[max(0, open_idx - 10)][0] - 0.5)


def find_closing(words, nw, silences, last, seg_start):
    """Find where a host-read ad ends. Returns end time or None."""
    # 1. thanks/support sign-off within 250 words after last brand mention;
    #    extend through the sign-off, then take the nearest pause.
    pauses = get_pauses(words, silences)
    for i in range(last, min(len(nw), last + 250)):
        window = " ".join(nw[i:i + 10])
        if "thank" in window and ("support" in window or "sponsor" in window):
            t = next((k for k in range(i, min(i + 10, len(nw))) if "thank" in nw[k]), i)
            return snap_forward(pauses, thanks_end(words, nw, t))
    # 2. first long silence after the last mention (don't run away with it)
    close_after = words[min(last + 5, len(words) - 1)][1]
    for s_start, s_end in silences:
        if s_start >= close_after and (s_end - s_start) >= 1.5:
            if s_start - seg_start <= MAX_AD_LEN and s_start - close_after <= 120:
                return s_start + 0.2
            break
    # 3. return cue within 400 words
    for i in range(last, min(len(words), last + 400)):
        window = " ".join(nw[i:i + 4])
        if any(cue in window for cue in RETURN_CUES):
            if words[i][0] - seg_start <= MAX_AD_LEN:
                return float(words[i][0])
    return None


def _first_opening_cue_after(words, nw, silences, t0, t1):
    """First OPEN_CUES occurrence starting in (t0, t1), snapped to the
    preceding sentence end (same snap as find_opening). Returns the snap
    time or None. Used by the imp2 merge guard: a cluster segment can
    start early on content mentions of a common-word sponsor
    ('function' on Huberman Emotions: cluster starts 57s before the
    ad), so the extension stops at the ad's actual opening cue
    instead of the cluster's first mention."""
    for i in range(len(nw)):
        if not (t0 < words[i][0] < t1):
            continue
        for j in range(i, min(i + 7, len(nw))):
            hit = None
            for cue in OPEN_CUES:
                k = len(cue.split())
                # Transcript words carry trailing punctuation ('sponsor,')
                # and norm_words does not strip it, so compare stripped.
                _w = [w.strip(".,!?;:'\"") for w in nw[j:j + k]]
                if " ".join(_w) == cue:
                    hit = cue
                    break
            if hit is None:
                continue
            cue_t = words[j][0]
            for w in reversed(words):
                if w[1] > cue_t:
                    continue
                if w[1] < cue_t - 30:
                    break
                if w[2].strip().endswith((".", "!", "?")):
                    return w[1]
            for p_start, p_end in reversed(get_pauses(words, silences)):
                if p_end <= cue_t and p_end >= cue_t - 30 and \
                        (p_end - p_start) >= 0.6:
                    # T2-N2: return the silence END (the speech-side
                    # boundary for a start edge), not its start.
                    return p_end
            return max(0.0, cue_t - 0.5)
    return None


def _word_votes(words, candidates):
    """Per-word vote counts from the method segments (2026-11-10,
    boundary-confidence test): a segment covers a word iff the word's
    start falls inside the segment (inclusive — the analog of the
    per-second heat map's inclusive second range). Returns a list of
    ints parallel to words. Used by the boundary-confidence score
    (_edge_sharpness)."""
    import bisect as _bisect
    starts = [w[0] for w in words]
    votes = [0] * len(words)
    for s, e, _l, _m in candidates:
        i0 = _bisect.bisect_left(starts, s)
        i1 = _bisect.bisect_right(starts, e)
        for i in range(i0, i1):
            votes[i] += 1
    return votes


def _edge_sharpness(starts, ends, votes, t, side):
    """Sharpness of a word boundary at time t (2026-11-10,
    boundary-confidence test): mean votes of 3 words just inside the
    edge minus mean votes of 3 words just outside. side='start':
    inside is the 3 words at/after t; side='end': inside is the 3
    words ending at/before t. Returns None when either window is
    empty (e.g. an edge at 0.0s has no outside words). Sharpness
    separates good from bad edges — flat edges (< 0.5) have median
    |err| ~14s vs ~2.7s for sharper edges on the 25-episode SB set —
    but the TRUE edge is itself usually flat (median sharpness 0.00),
    so sharpness flags bad edges without locating the truth."""
    import bisect as _bisect
    n = len(votes)
    if side == "start":
        i0 = _bisect.bisect_left(starts, t)
        for _c in (i0 - 1, i0):
            if 0 <= _c < n and (i0 >= n or
                                abs(starts[_c] - t) < abs(starts[i0] - t)):
                i0 = _c
        inside = votes[i0:i0 + 3]
        outside = votes[max(0, i0 - 3):i0]
    else:
        j0 = _bisect.bisect_left(ends, t)
        for _c in (j0 - 1, j0):
            if 0 <= _c < n and (j0 >= n or
                                abs(ends[_c] - t) < abs(ends[j0] - t)):
                j0 = _c
        inside = votes[max(0, j0 - 2):j0 + 1]
        outside = votes[j0 + 1:j0 + 4]
    if not inside or not outside:
        return None
    return sum(inside) / len(inside) - sum(outside) / len(outside)


# Weak-evidence block labels (2026-11-15, A/B test — NOT merged):
# a block's chapter title names a sponsor, and a wrong name is
# user-visible harm. A block is WEAK when no naming method's vote
# names its sponsor. Naming methods (their candidate labels name an
# advertiser): brand, brand-orphan, cta, url, cluster,
# cluster-popular, chapter. Weak methods (their labels are artifacts
# that name no advertiser): duplicate, reverb, voice,
# history-preroll — labels 'dry-island', 'voice-anomaly',
# 'history-prior', 'duplicate-N'. A weak block's label is rewritten
# to '<methods>:ad'; 'ad' is in GENERIC_AD_TITLES, so the existing
# ad_chapter_name() returns None and the chapter is titled plain
# 'Muse Detected Ad'. Spans, votes, block existence, and every other
# label are untouched; suspects are untouched. No sponsor name is
# hardcoded: the rule reads only the method list and the artifact
# set from the label string. See sb_eval/ab/label_quality/NOTE.md.
_WEAK_LABEL_METHODS = frozenset(
    {"duplicate", "reverb", "voice", "history-preroll"})
_WEAK_ARTIFACT_NAMES = frozenset(
    {"dry-island", "voice-anomaly", "history-prior"})


def _is_generic_part(part):
    """True when a label part names no advertiser and must never serve
    as ad identity in a same-ad comparison (T2-N3): 'ad' (the
    weak-strip / own-identity-strip output), the weak-artifact names,
    and 'duplicate-N'. A part is normalised like _xep_part (lower,
    spaces stripped); normalise again here so callers can pass a raw
    split(':')[-1] part."""
    import re as _re
    if not part:
        return False
    p = part.strip().lower().replace(" ", "")
    if p == "ad" or p in _WEAK_ARTIFACT_NAMES:
        return True
    return _re.fullmatch(r"duplicate-\d+", p) is not None


def _is_weak_label(label):
    """True when a block label rests only on weak evidence."""
    import re as _re
    if not label:
        return False
    spons = label.split(":")[-1].strip().lower().replace(" ", "")
    if spons in _WEAK_ARTIFACT_NAMES or \
            _re.fullmatch(r"duplicate-\d+", spons):
        return True
    if ":" not in label:
        return False
    methods = [m for m in label.split(":", 1)[0].split("/") if m]
    return bool(methods) and all(m in _WEAK_LABEL_METHODS for m in methods)


def _strip_weak_labels(blocks):
    """Rewrite weak block labels to '<methods>:ad', preserving every
    other field. Blocks are (start, end, label, *rest)."""
    out = []
    for b in blocks:
        label = b[2]
        if _is_weak_label(label):
            label = label.split(":", 1)[0] + ":ad" if ":" in label else "ad"
        out.append((b[0], b[1], label) + tuple(b[3:]))
    return out


# URL-stem guard (2026-11-15, A/B test — NOT merged): a spoken-URL
# stem ('friedman' from Whisper-split "Lex Friedman .com", a
# mangled own-domain stem like 'lexfreedman' / 'hubertmanlab')
# can slip the exact-match own-domain filter and then resolve,
# via ad_chapter_name's fuzzy fallback (>= 0.75), to the SHOW
# ITSELF — because the show's own domain is itself listed as a
# description sponsor. A block label is stripped iff (i) its
# methods include 'url' or 'cta' (the name comes from URL
# evidence), (ii) its sponsor part is not exactly a description
# sponsor's label, and (iii) its normalized sponsor part
# fuzzy-matches an own-identity form at >= 0.75 — the pipeline's
# own fuzzy threshold, not a tuned number. Own identity =
# {_domain_stem(channel_link)} ∪ own_domains, feed-supplied,
# never hardcoded. A stripped label becomes '<methods>:ad'; 'ad'
# is in GENERIC_AD_TITLES, so the chapter is titled plain
# "Muse Detected Ad". Spans, votes, block existence, and every
# other label are untouched; suspects are untouched. See
# sb_eval/ab/url_stem_guard/NOTE.md.
_URL_GUARD_METHODS = frozenset({"url", "cta"})
_URL_GUARD_THRESHOLD = 0.75


def _is_own_identity_label(label, sponsors, own_identity):
    """True when a block's sponsor name comes from URL evidence
    and fuzzy-matches the podcast's own identity."""
    import difflib as _difflib
    import re as _re
    if not label or not own_identity or ":" not in label:
        return False
    methods = [m for m in label.split(":", 1)[0].split("/") if m]
    if not any(m in _URL_GUARD_METHODS for m in methods):
        return False
    spons = _re.sub(r"[^a-z0-9]", "", label.split(":")[-1].strip().lower())
    if not spons:
        return False
    sponsor_labels = {_re.sub(r"[^a-z0-9]", "", sp.get("label", "").lower())
                      for sp in (sponsors or [])}
    if spons in sponsor_labels:
        return False
    for ident in own_identity:
        ident_n = _re.sub(r"[^a-z0-9]", "", str(ident).lower())
        if not ident_n:
            continue
        if _difflib.SequenceMatcher(None, spons, ident_n).ratio() \
                >= _URL_GUARD_THRESHOLD:
            return True
    return False


def _strip_own_identity_labels(blocks, sponsors, own_identity):
    """Rewrite own-identity block labels to '<methods>:ad',
    preserving every other field. Blocks are (start, end, label,
    *rest)."""
    out = []
    for b in blocks:
        label = b[2]
        if _is_own_identity_label(label, sponsors, own_identity):
            label = label.split(":", 1)[0] + ":ad" if ":" in label else "ad"
        out.append((b[0], b[1], label) + tuple(b[3:]))
    return out


def detect_ad_blocks(words, sponsors, silences, mp3_path=None, chapters=None,
                     own_domains=None, candidates_out=None, own_identity=None,
                     audio_duration=None):
    # words: list of (start, end, word) from Whisper (may contain duplicates).
    """Locate ad-read blocks via multi-method heat map (2026-10-09).

    Each detection method runs independently on the unedited episode,
    then results are cross-checked via a per-second vote heat map.
    Segments with 2+ method votes are high-confidence ads.
    Single-method hits are kept only if that method is high-precision.

    SB-eval improvements (2026-11-07, validated on 25 SponsorBlock-
    anchored episodes): (1) same-label merge — a single-method segment
    overlapping a heat-map block for the same sponsor extends the
    block's start to the segment's start, stopped at the ad's opening
    cue when one is spoken in between (fixes tail-only blocks where
    the story opening carries no brand/URL/CTA evidence); (2)
    duplicate-only blocks are never marked alone — a duplicate segment
    must corroborate another method's vote (false positives on
    repeated intros and re-read quotes); (3) the show's own domains
    (derived generically from the RSS channel link and non-sponsor
    description links) are excluded from URL evidence.

    Methods:
    - cta: Generic URL/promo-code CTA patterns
    - brand: Description sponsor brand mentions
    - cluster: Dense brand-mention clusters (casual ad reads)
    - cluster-popular: Dense mentions of popular sponsors not in description
      (low-confidence: auto-marked only with 2+ votes)
    - duplicate: Verbatim-repeated segments (inserted ads run 2+ times)
    - voice: Host-voice anomaly (segments far from host voice profile)
    - chapter: Publisher-labeled ad chapters, e.g. '[Ad]', 'Sponsor'
      (2026-10-14; high-precision like brand/cta)
    - brand-orphan: Sponsor mentions _expand_segment could not expand
      (no opening cue). Low-confidence: heat-map votes only, never marks
      alone (2026-10-14; Unsubscribe 268 DOSE/AG1 were silently dropped)
    - reverb: Room-echo dryness vote (2026-10-27; the Unsubscribe 286
      idea). Gated to reverberant episodes (median lateAC >= 0.25) with an
      adaptive threshold, so it stays silent on dry professional shows.
      Low-confidence: heat-map votes only, never marks alone.
    - cta phone: toll-free numbers ("1-800-GOT-Junk") are CTA anchors like
      URLs, with a tight-cluster expansion fallback for produced spots
      with no host cue (2026-10-27).
    - url: spoken URLs with NO CTA phrase (2026-10-28; Unsubscribe 287
      Leaf Filter says 'at leaffilter.com slash gold'). Low-confidence:
      heat-map votes only, never marks alone. Also names the
      advertiser for the block-label picker.

    Heat-map blocks (2+ votes) may be as short as 30s so 30-31s dynamic
    spots can cut; single-method blocks keep the 45s floor. Heat-map
    blocks separated by < 20s are merged when they carry the same ad
    label (merging across a label change fused 287 Leaf Filter #2 into
    the Poncho block, which the cap then trimmed away). Sponsor-identified blocks are
    capped to the sponsor's in-block mention span +-30s (2026-10-27;
    Unsubscribe 286 over-cut 312s without it), and a sponsor-labeled
    heat-map block with no mention of that sponsor within 10s is dropped
    as padding/dryness noise. Finally (2026-10-29, middle ground):
    each block edge is refined inward to the nearest method-segment
    boundary lying between the block edge and the nearest in-block
    brand mention — CTA/URL ends are trimmed to their closing
    evidence sentence for edge purposes (starts stay raw, since an
    opening cue precedes the first brand word; voting uses the raw
    segments). Edges with no segment boundary in the interval stay
    put, and the refined span always contains the mention span, so
    reads that name their sponsor sparsely keep their full span.
    Block labels are picked, not alphabetical:
    a mention-anchored sponsor label first, then a label with a
    spoken-URL anchor in the region, then any non-artifact label
    ('dry-island', 'voice-anomaly', 'duplicate-N' name no advertiser).

    Returns (blocks, suspects). Blocks are (start, end, label,
    start_sharpness, end_sharpness): each edge carries its boundary-
    confidence sharpness (2026-11-10, KEPT as a flag — flat edges
    < 0.5 are likely bad and are flagged, never moved; None when the
    window is empty). Suspects are low-confidence candidates
    (voice, cluster-popular, brand-orphan, reverb) that did NOT become marked blocks:
    plausible ad-length segments the pipeline was unsure about. They are
    packaged into a suspect reel (2026-10-22, per the owner) for owner review of
    minutes of uncertain audio instead of the whole episode. A suspect
    must not overlap a marked block — an overhang of a marked ad is not a
    separate suspect.
    """
    # m13: sort + dedupe once here so every method below (and the
    # words[-1] duration sites) sees non-decreasing starts.
    words = _sorted_words(words)
    # Run each method independently, collecting (start, end, label, method)
    # Empty transcript: no detection is possible, and the chapter
    # method (plus the words[-1] sites below) would index words[-1]
    # (M21). Return before any method runs.
    if not words:
        return [], []
    # T2-N7: the detection horizon is the audio duration, not the last
    # transcribed word. audio_duration is the probe duration passed by
    # the production call sites; None keeps the old word horizon.
    # max() keeps the word horizon as a floor for a stale/short value.
    # (Placed after the empty-words return: words[-1] is only safe here.)
    # T2-N7 guard: _word_horizon is the pre-N7 horizon, for the
    # horizon-extension content gate at block formation below.
    _word_horizon = int(words[-1][0]) + 1
    _horizon = max(_word_horizon, int(audio_duration) + 1) \
        if audio_duration else _word_horizon
    candidates = []
    candidates.extend([(s, e, l, 'cta') for s, e, l in _method_cta(words, sponsors, silences, own_domains)])
    candidates.extend([(s, e, l, 'url') for s, e, l in _method_url(words, silences, own_domains)])
    candidates.extend([(s, e, l, 'brand') for s, e, l in _method_brand(words, sponsors, silences)])
    candidates.extend([(s, e, l, 'brand-orphan') for s, e, l in _method_brand_orphan(words, sponsors, silences)])
    candidates.extend([(s, e, l, 'cluster') for s, e, l in _method_cluster(words, sponsors, silences)])
    candidates.extend([(s, e, l, 'cluster-popular') for s, e, l in _method_cluster_popular(words, sponsors, silences)])
    candidates.extend([(s, e, l, 'duplicate') for s, e, l in find_duplicate_segments(words)])
    candidates.extend([(s, e, l, 'chapter') for s, e, l in _method_chapter(words, chapters, silences, audio_duration=audio_duration)])
    if mp3_path:
        candidates.extend([(s, e, l, 'voice') for s, e, l in _method_voice(words, mp3_path)])
        candidates.extend([(s, e, l, 'reverb') for s, e, l in _method_reverb(words, mp3_path)])
    candidates.extend([(s, e, l, 'history-preroll') for s, e, l in _method_history_preroll(words)])

    if not candidates:
        if candidates_out is not None:
            candidates_out.extend(candidates)
        return [], []

    # Spoken-URL positions (brand stem, time) for the label picker and
    # the single-cluster anchor check. Own-domain URLs are filtered
    # here too (imp5): they must not name a block or anchor a cluster.
    _nw_heat = norm_words(words)
    _bare_times = [(b, words[i][0]) for b, i in find_bare_urls(_nw_heat, own_domains)]

    def _pick_label(entries, bs, be):
        """Pick the block label for a heat-map region.

        The old rule (alphabetically first vote label) misnamed real
        ads: 287 Leaf Filter became 'Indeed' (a fuzzy 'need' collision)
        and 287 Ridge / 286 Got Junk became 'dry-island' (a method
        artifact). Prefer (1) a description-sponsor label with a mention
        inside the region — the anchor check drops sponsor-labeled
        blocks without one, so a mislabeled region would die; then
        (2) a non-artifact label with a spoken-URL anchor in the
        region (bare URLs name the advertiser: 'leaffilter', 'ridge');
        then (3) any non-artifact label; else the first artifact label.
        Artifact labels name no advertiser: 'dry-island',
        'voice-anomaly', 'duplicate-N'.
        """
        import re as _re
        labels = sorted(set(l for m, l in entries))

        def _part(l):
            if not l:
                return ""
            return l.split(':')[-1].strip().lower().replace(' ', '')

        def _artifact(l):
            p = _part(l)
            return p in ('dry-island', 'voice-anomaly', 'history-prior') or \
                _re.fullmatch(r'duplicate-\d+', p) is not None
        for l in labels:
            if _artifact(l):
                continue
            p = _part(l)
            for sp in sponsors:
                if sp['label'].lower().replace(' ', '') != p:
                    continue
                hits = find_mentions(_nw_heat, sp['variants'])
                if any(bs - 10 <= words[h][0] <= be + 10 for h in hits):
                    return l
        for l in labels:
            if _artifact(l):
                continue
            p = _part(l)
            if any(bs <= t <= be and b.replace(' ', '') == p
                   for b, t in _bare_times):
                return l
        for l in labels:
            if not _artifact(l):
                return l
        return labels[0] if labels else 'ad'

    # History-vote bridging guard (fusion fix, 2026-11-14, A/B test):
    # a corroborating vote must never CONNECT two baseline regions
    # carrying different ad labels — that fuses two back-to-back ads
    # into one block (Huberman neuroscience: the history vote bridged
    # the 5s dip between the Helix Sleep and Rorra regions, 1039-1044,
    # and the Rorra read was trimmed away). Compute the baseline
    # regions (>=2 votes, contiguous) from the non-history candidates
    # and their labels via _pick_label; where the history vote would
    # connect two adjacent regions with different labels — i.e. every
    # second of the gap between them already carries >=1 vote — punch
    # a hole in the history segment over the gap. A gap containing a
    # 0-vote second cannot be connected by a single vote anyway, so no
    # hole is needed there; that is what leaves the vote working in
    # open territory, where it extends a region or seeds a new one
    # next to it (the Our Place gain). Same-label pairs are left
    # alone: the pipeline's gap-merge already joins them (<20s).
    _hist_pos = [i for i, c in enumerate(candidates)
                 if c[3] == 'history-preroll']
    if _hist_pos:
        _gd = _horizon
        _bv = [0] * _gd
        _bdet = [[] for _ in range(_gd)]
        for _cs, _ce, _cl, _cm in candidates:
            if _cm == 'history-preroll':
                continue
            for _sec in range(int(_cs), min(int(_ce) + 1, _gd)):
                _bv[_sec] += 1
                _bdet[_sec].append((_cm, _cl))
        _gregs = []
        _in_r = False
        for _sec in range(_gd):
            if _bv[_sec] >= 2 and not _in_r:
                _in_r, _rbs = True, _sec
            elif _bv[_sec] < 2 and _in_r:
                _in_r = False
                _gregs.append((_rbs, _sec))
        if _in_r:
            _gregs.append((_rbs, _gd))
        _holes = []
        for (_a0s, _a0e), (_a1s, _a1e) in zip(_gregs, _gregs[1:]):
            _ent0 = sum([_bdet[_x] for _x in range(_a0s, _a0e)], [])
            _ent1 = sum([_bdet[_x] for _x in range(_a1s, _a1e)], [])
            _lab0 = _pick_label(_ent0, _a0s, _a0e)
            _lab1 = _pick_label(_ent1, _a1s, _a1e)
            if _lab0.split(':')[-1].strip().lower() == \
                    _lab1.split(':')[-1].strip().lower():
                continue
            if all(_bv[_x] >= 1 for _x in range(_a0e, _a1s)):
                _holes.append((_a0e, _a1s))
        if _holes:
            _new_cands = []
            for _i, _c in enumerate(candidates):
                if _i not in _hist_pos:
                    _new_cands.append(_c)
                    continue
                _pieces = [(_c[0], _c[1])]
                for _hs, _he in _holes:
                    _out = []
                    for _ps, _pe in _pieces:
                        if _he <= _ps or _hs >= _pe:
                            _out.append((_ps, _pe))
                        else:
                            if _ps < _hs:
                                _out.append((_ps, _hs))
                            if _he < _pe:
                                _out.append((_he, _pe))
                    _pieces = _out
                for _ps, _pe in _pieces:
                    if _pe - _ps >= 1:
                        _new_cands.append((_ps, _pe, _c[2], _c[3]))
            candidates = _new_cands

    # Build heat map: per-second vote counts
    duration = _horizon
    votes = [0] * duration
    vote_detail = [[] for _ in range(duration)]  # which methods voted
    for s, e, label, method in candidates:
        for sec in range(int(s), min(int(e) + 1, duration)):
            votes[sec] += 1
            vote_detail[sec].append((method, label))

    # High-confidence: 2+ methods agree. Merge contiguous.
    blocks = []
    in_block = False
    bs = 0
    block_methods = set()
    for sec in range(duration):
        if votes[sec] >= 2 and not in_block:
            in_block = True
            bs = sec
            block_methods = set(m for m, _ in vote_detail[sec])
        elif votes[sec] >= 2 and in_block:
            block_methods.update(m for m, _ in vote_detail[sec])
        elif votes[sec] < 2 and in_block:
            in_block = False
            be = sec
            dur = be - bs
            if HEATMAP_MIN_AD_LEN <= dur <= MAX_AD_LEN:
                # T2-N7 guard: a block that only reaches the floor
                # because of votes past the last transcribed word
                # (its 2+-vote run inside the word horizon is below
                # the floor) must carry a content vote in the block —
                # a CTA, brand mention, cluster, duplicate, publisher
                # chapter, or orphan mention. A region with only a
                # spoken URL and a voice/reverb anomaly past the last
                # word is a sign-off cross-promo, not a paid ad: drop
                # it (it stays a suspect, as before N7).
                if min(be, _word_horizon) - bs < HEATMAP_MIN_AD_LEN and \
                        not (block_methods & _CONTENT_METHODS):
                    log(f"  heatmap {bs:.0f}s-{be:.0f}s ({dur:.0f}s) votes={sorted(block_methods)} dropped: horizon-dependent, no content evidence")
                else:
                    entries = sum([vote_detail[s] for s in range(bs, be)], [])
                    blocks.append((bs, be, f"{'/'.join(sorted(block_methods))}:{_pick_label(entries, bs, be)}"))
                    log(f"  heatmap {bs:.0f}s-{be:.0f}s ({dur:.0f}s) votes={sorted(block_methods)}")
    if in_block:
        be = duration
        dur = be - bs
        if HEATMAP_MIN_AD_LEN <= dur <= MAX_AD_LEN:
            if min(be, _word_horizon) - bs < HEATMAP_MIN_AD_LEN and \
                    not (block_methods & _CONTENT_METHODS):
                log(f"  heatmap {bs:.0f}s-{be:.0f}s ({dur:.0f}s) votes={sorted(block_methods)} dropped: horizon-dependent, no content evidence")
            else:
                entries = sum([vote_detail[s] for s in range(bs, be)], [])
                blocks.append((bs, be, f"{'/'.join(sorted(block_methods))}:{_pick_label(entries, bs, be)}"))

    # Gap-merge heat-map blocks (2026-10-27): dryness islands are
    # fragmented, so a single ad's 2+ regions can split (Unsubscribe 286
    # NOBL: 2970-3020 + 3030-3065; Perfect Jean: 5130-5171 + 5190-5236
    # with a 19s gap). Merge regions separated by < 20s.
    merged_blocks = []
    for bs, be, l in blocks:
        # Merge only same-ad regions (2026-10-28): merging across a
        # label change let 287 Leaf Filter #2 (labeled PONCHO by the
        # old picker) fuse into the Poncho block, which the
        # mention-span cap then trimmed away — eating a real ad.
        # T2-N3: never merge on a generic part alone — every
        # weak-stripped block has part 'ad' and every voice/reverb-only
        # region has part 'dry-island', so equal generic parts are
        # not evidence of one ad.
        if merged_blocks and bs - merged_blocks[-1][1] < 20 and \
                be - merged_blocks[-1][0] <= MAX_AD_LEN and \
                merged_blocks[-1][2].split(':')[-1] == l.split(':')[-1] and \
                not _is_generic_part(l.split(':')[-1]):
            ps, pe, pl = merged_blocks[-1]
            merged_blocks[-1] = (ps, max(pe, be), pl)
        else:
            merged_blocks.append((bs, be, l))
    blocks = merged_blocks

    # Sponsor-anchor check (2026-10-27): a heat-map block labeled with a
    # description sponsor must sit within 10s of an actual mention of
    # that sponsor. Otherwise its votes came from padding, not the ad —
    # orphan windows extend 45s past mentions, and a dryness island
    # overlapping that padding fabricates a 2-vote region (Unsubscribe
    # 286: a 32s false block at 2914-2946, 13s before the first NOBL
    # mention). Drop such blocks; single-method blocks are unaffected
    # (except single-method cluster blocks, which get their own spoken-
    # URL anchor check in the single-method loop below).
    anchored = []
    nw_all = norm_words(words)
    for bs, be, l in blocks:
        sp_part = l.split(':')[-1].strip().lower()
        match = None
        for sp in sponsors:
            if sp['label'].lower() == sp_part:
                match = sp
                break
        if match:
            hits = find_mentions(nw_all, match['variants'])
            near = [words[h][0] for h in hits
                    if bs - 10 <= words[h][0] <= be + 10]
            if not near:
                log(f"  drop unanchored heatmap block {bs:.0f}s-{be:.0f}s "
                    f"('{match['label']}' — no mention within 10s)")
                continue
        anchored.append((bs, be, l))
    blocks = anchored

    # Single-method high-precision: CTA, cluster, brand, and chapter
    # are reliable alone. Voice, cluster-popular, and brand-orphan
    # alone have too many false positives — they only count via heat-map
    # 2+ votes. Duplicate is NOT in this list (2026-11-07, SB-eval
    # improvement 4): duplicate-only blocks are false positives on
    # pure content (repeated intros, re-read quotes), so a duplicate
    # segment must corroborate another method's vote in the heat map
    # to mark. Removing it cut false blocks 67 -> 45 on the 25-episode
    # SB eval at a cost of 1 matched label.
    _single_methods = ('cta', 'cluster', 'brand', 'chapter')
    # Indices of blocks whose start the same-label merge moved (2026-11-09,
    # brand-orphan test): the orphan pass below must not double-extend them.
    _merge_extended = set()
    for s, e, label, method in candidates:
        if method in _single_methods:
            dur = e - s
            if not (MIN_AD_LEN <= dur <= MAX_AD_LEN):
                continue
            # Single-method cluster blocks need a spoken URL for that
            # sponsor nearby (2026-10-28): dense mentions alone can be
            # a place name — 287 'rattlesnake Ridge' produced a 77s
            # false block. Brand blocks keep cue-based precision. The
            # stem match tolerates domain labels ('bitwarden.com' vs
            # stem 'bitwarden'), label components ('Club TWiT' vs stem
            # 'twit'), and Whisper stem mangling ('manscape' vs
            # 'manscaped', 0.94). (Checked before the merge below, so
            # an unanchored cluster segment can never extend a block.)
            if method == 'cluster':
                import re as _re2
                from difflib import SequenceMatcher as _SM
                _tlds = URL_TLDS  # single shared set (M15)
                _p = label.lower().replace(' ', '')
                _allowed = {_p}
                for _sp in sponsors:
                    if _sp['label'].lower().replace(' ', '') != _p:
                        continue
                    for _v in [_sp['label']] + list(_sp.get('variants', [])):
                        _norm = _v.lower().replace(' ', '')
                        _allowed.add(_norm)
                        for _comp in _re2.split(r'[.\s]+', _v.lower()):
                            if _comp and _comp not in _tlds:
                                _allowed.add(_comp)
                _anchored = False
                for _b, _t in _bare_times:
                    if not (s - 10 <= _t <= e + 10):
                        continue
                    if _b in _allowed or any(
                            _SM(None, _b, _a).ratio() >= 0.85
                            for _a in _allowed):
                        _anchored = True
                        break
                if not _anchored:
                    log(f"  drop unanchored single-cluster block "
                        f"{s:.0f}s-{e:.0f}s ('{label}' — no spoken URL)")
                    continue
            # Same-label merge (2026-11-07, SB-eval improvement 2):
            # if this segment overlaps a heat-map block for the SAME
            # sponsor, extend that block's start to the segment's start
            # instead of dropping the segment as "already covered".
            # The segment (often the cluster, whose mentions span the
            # full read) frequently starts where the ad starts —
            # Function on Huberman gut-health: cluster 4553.7s vs
            # heat-map block 4597.1s (+43.6s late) — while the heat-map
            # block starts only where a second vote arrives (the CTA/URL
            # tail). Dropping the segment is what makes tail-only
            # blocks. The mention-span cap below still bounds the
            # extension to the mention span +-30s. The end is NOT
            # merged: a cluster segment can end late on content
            # mentions after the ad (Whoop on Sydney Sweeney: cluster
            # ends 1448.9s, 57s past the read), and the head-extension
            # variant tested 2026-11-07 was a wash (Modern Wisdom
            # unchanged — those errors are label artifacts).
            # Guard: a cluster segment can start early on content
            # mentions of a common-word sponsor, so stop the extension
            # at the ad's actual opening cue when one is spoken between
            # the segment start and the block start (never earlier than
            # the segment start). A silence-based start fallback was
            # tested and rejected the same day: on Huberman Extinct
            # Species it would extend the Eight Sleep block 73s backward
            # into the preceding Joovv read.
            _part = (label or "").split(':')[-1].strip().lower().replace(' ', '')
            _merged = False
            for _bi, (_bs, _be, _bl) in enumerate(blocks):
                if _bl.split(':')[-1].strip().lower().replace(' ', '') != _part:
                    continue
                if not (s < _be and e > _bs):
                    continue
                _ns = _bs
                if s < _bs:
                    _ns = s
                    _cue = _first_opening_cue_after(words, _nw_heat,
                                                    silences, s, _bs)
                    if _cue is not None:
                        _ns = max(s, _cue)
                if _ns != _bs:
                    log(f"  extend '{label}': {_bs:.0f}s-{_be:.0f}s -> "
                        f"{_ns:.0f}s-{_be:.0f}s (overlap {method})")
                    blocks[_bi] = (_ns, _be, _bl)
                    _merge_extended.add(_bi)
                _merged = True
                break
            if _merged:
                continue
            # Skip if already covered by a heatmap block
            if any(s < be and e > bs for bs, be, _ in blocks):
                continue
            # Skip duplicates (same label already added)
            if any(label == l for _, _, l in blocks):
                continue
            blocks.append((s, e, label))
            log(f"  single-method '{label}': {method} {s:.1f}s-{e:.1f}s ({dur:.0f}s)")

    # Brand-orphan merge (2026-11-09, SB-eval improvement 6, validated
    # on 25 SponsorBlock-anchored episodes):
    # a brand-orphan segment — a sponsor mention _expand_segment could
    # not turn into a block — is the only evidence for reads that name
    # their sponsor once, so no single-method segment overlaps the
    # heat-map block with an earlier start and the imp2 merge never
    # fires; the block stays tail-only. Extend such a block to the
    # orphan segment's bounds (already silence-snapped at +-45s around
    # the mention span), same sponsor only, with the same cue guard as
    # the imp2 merge on the start side and a return-to-content cue /
    # different-sponsor guard on the end side. The mention-span cap
    # below still bounds both extensions to mention span +-30s, exactly
    # as it bounds imp2. A block the imp2 merge already extended is
    # never extended again. An unbounded silence-based extension was
    # tested and rejected 2026-11-07 (Eight Sleep would extend 73s
    # backward into the preceding Joovv read), so the extension target
    # here is always the same-sponsor orphan segment, never a raw
    # silence gap.
    for s, e, label, method in candidates:
        if method != 'brand-orphan':
            continue
        _part = (label or "").split(':')[-1].strip().lower().replace(' ', '')
        for _bi, (_bs, _be, _bl) in enumerate(blocks):
            if _bl.split(':')[-1].strip().lower().replace(' ', '') != _part:
                continue
            if _bi in _merge_extended:
                continue
            if not (s < _be and e > _bs):
                continue
            _ns, _ne = _bs, _be
            if s < _bs:
                _ns = s
                _cue = _first_opening_cue_after(words, _nw_heat,
                                                silences, s, _bs)
                if _cue is not None:
                    _ns = max(s, _cue)
                # Never extend into a preceding block.
                for (_bs2, _be2, _bl2) in blocks:
                    if _be2 <= _bs and _be2 > _ns:
                        _ns = max(_ns, _be2)
            if e > _be:
                _ne = e
                # Return-to-content cue: extend only to the first
                # RETURN_CUES phrase spoken after the block end.
                for _i in range(len(_nw_heat)):
                    if not (_be < words[_i][0] < e):
                        continue
                    _win = " ".join(_nw_heat[_i:_i + 4])
                    if any(_c in _win for _c in RETURN_CUES):
                        _ne = min(_ne, words[_i][0])
                        break
                # Different-sponsor guard (imp3 fallback rule): if
                # another sponsor is mentioned between the block end
                # and the target end, the end is a different ad —
                # do not extend at all.
                for _sp2 in sponsors:
                    if _sp2['label'].lower().replace(' ', '') == _part:
                        continue
                    _hits2 = find_mentions(_nw_heat, _sp2['variants'])
                    if any(_be < words[_h][0] < _ne for _h in _hits2):
                        _ne = _be
                        break
                # Never extend into a following block.
                for (_bs2, _be2, _bl2) in blocks:
                    if _bs2 > _be and _bs2 < _ne:
                        _ne = _bs2
            if _ns != _bs or _ne != _be:
                log(f"  orphan-extend '{label}': {_bs:.0f}s-{_be:.0f}s -> "
                    f"{_ns:.0f}s-{_ne:.0f}s")
                blocks[_bi] = (_ns, _ne, _bl)
            break

    # Mention-span cap (2026-10-27): orphan padding (+-45s) and cluster
    # pause-snapping inflate blocks with surrounding content (Unsubscribe
    # 286: 312s over-cut across 4 blocks). Trim each sponsor-identified
    # block to the sponsor's in-block mention span +-30s. Only shrinks,
    # never expands; validated to keep full coverage on all 4 cut 286 ads.
    nw = norm_words(words)
    capped = []
    for s, e, l in blocks:
        sp_part = l.split(':')[-1].strip().lower()
        match = None
        for sp in sponsors:
            if sp['label'].lower() == sp_part:
                match = sp
                break
        if match:
            hits = find_mentions(nw, match['variants'])
            inblock = [words[h][0] for h in hits if s - 60 <= words[h][0] <= e + 60]
            if inblock:
                ns = max(s, min(inblock) - 30)
                ne = min(e, max(inblock) + 30)
                if ne - ns >= MIN_AD_LEN and (ns != s or ne != e):
                    log(f"  cap '{match['label']}': {s:.0f}s-{e:.0f}s -> "
                        f"{ns:.0f}s-{ne:.0f}s")
                    s, e = ns, ne
        capped.append((s, e, l))
    blocks = capped

    # Edge refinement (2026-10-29, middle ground — replaces the
    # same-day shrink, which collapsed spans to the mention span and
    # gutted reads that name their sponsor sparsely: Stop Box 51s ->
    # 13s, Club TWiT 75s -> 2s). For each block edge, look at the
    # interval between the block edge and the nearest in-block brand
    # mention; if any method segment starts (start edge) or ends
    # (end edge) inside that interval, move the edge to the closest
    # one. Otherwise the edge stays. CTA/URL bounds are asymmetric
    # for edge purposes only (voting still uses the raw segments):
    # starts stay raw, because an opening cue precedes the first
    # brand word (SuperPower's cue sits 11s before its first
    # mention); ends are trimmed to the closing evidence sentence,
    # because expansions overshoot past it (Got Junk's CTA ran 13s
    # past '.com!'). This repairs cap padding (Poncho 28s,
    # SuperPower 18s) and segment overshoot while keeping the read
    # body: Stop Box's tail and Club TWiT's plug survive because
    # their bounding segments still bound them, and the refined span
    # always contains the mention span. Generic: no podcast, show,
    # or episode logic anywhere.
    edge_cands = []
    for cs, ce, cl, cm in candidates:
        if cm in ('cta', 'url'):
            lab = cl.lower()
            _, ce = _evidence_sentence_trim(words, nw, cs, ce, {lab, lab.replace(' ', '')})
        edge_cands.append((cs, ce))
    refined = []
    for s, e, l in blocks:
        _os, _oe = s, e
        sp_part = l.split(':')[-1].strip().lower()
        match = None
        for sp in sponsors:
            if sp['label'].lower() == sp_part:
                match = sp
                break
        variants = match['variants'] if match else [sp_part]
        hits = find_mentions(nw, variants)
        inblock = [words[h][0] for h in hits if s <= words[h][0] <= e]
        if inblock:
            ev_start, ev_end = min(inblock), max(inblock)
            starts = [cs for cs, _ in edge_cands if s <= cs <= ev_start]
            if starts:
                ns = max(starts)
                if ns != s:
                    log(f"  refine start '{l}': {s:.0f}s -> {ns:.0f}s")
                    s = ns
            ends = [ce for _, ce in edge_cands if ev_end <= ce <= e]
            if ends:
                ne = min(ends)
                if ne != e:
                    log(f"  refine end '{l}': {e:.0f}s -> {ne:.0f}s")
                    e = ne
        # Floor re-check (C2): refinement only moves edges inward,
        # so a block can leave it far below the floors (67s -> 6.5s).
        # Below the lowest floor (30s), keep the pre-refinement edges
        # — the block had 2+ votes over its full span.
        if e - s < HEATMAP_MIN_AD_LEN:
            log(f"  refine '{l}': {_os:.0f}s-{_oe:.0f}s -> {s:.0f}s-"
                f"{e:.0f}s below floor; keeping original edges")
            s, e = _os, _oe
        refined.append((s, e, l))
    blocks = refined

    # Final length validation (M25): the same-label and brand-orphan
    # merges above can push a block past MAX_AD_LEN. Drop over-long
    # blocks — a clamp would invent an edge with no evidence, and
    # every other duration gate in this file drops.
    _checked = []
    for s, e, l in blocks:
        if e - s > MAX_AD_LEN:
            log(f"  drop block {s:.0f}s-{e:.0f}s ('{l}'): "
                "exceeds MAX_AD_LEN")
            continue
        _checked.append((s, e, l))
    blocks = _checked

    # Suspect reel (2026-10-22, per the owner): low-confidence candidates
    # that did not become marked blocks. Replaces the old cluster-popular
    # LOW-CONFIDENCE log lines — the suspect reel is now the surface
    # where these get reviewed.
    suspects = []
    for s, e, label, method in candidates:
        if method not in ('voice', 'cluster-popular', 'brand-orphan', 'reverb', 'url'):
            continue
        dur = e - s
        if not (MIN_AD_LEN <= dur <= MAX_AD_LEN):
            continue
        if any(s < be and e > bs for bs, be, _ in blocks):
            continue
        suspects.append((s, e, f"{method}:{label}"))
        log(f"  SUSPECT '{label}': {method} {s:.1f}s-{e:.1f}s ({dur:.0f}s) — not cut (single vote)")
    # Merge overlapping suspects, keeping the first label
    suspects.sort()
    merged = []
    for s, e, l in suspects:
        if merged and s <= merged[-1][1]:
            ps, pe, pl = merged[-1]
            merged[-1] = (ps, max(pe, e), pl)
        else:
            merged.append((s, e, l))
    suspects = merged

    blocks.sort()
    # Boundary-confidence score (2026-11-10, KEPT as a flag): each
    # block edge carries its sharpness — mean votes of 3 words just
    # inside the edge minus mean votes of 3 words just outside.
    # Flat edges (< 0.5) are likely bad (median |err| ~14s vs ~2.7s
    # for sharper edges on the 25-episode SB set) and are flagged for
    # review; the score also feeds the future boundary scorer. It
    # never moves an edge. None when a window is empty (edge at the
    # episode boundary). Blocks are (start, end, label,
    # start_sharpness, end_sharpness).
    _votes = _word_votes(words, candidates)
    _starts = [w[0] for w in words]
    _ends = [w[1] for w in words]
    scored = []
    for s, e, l in blocks:
        _ss = _edge_sharpness(_starts, _ends, _votes, s, "start")
        _es = _edge_sharpness(_starts, _ends, _votes, e, "end")
        scored.append((s, e, l,
                       round(_ss, 2) if _ss is not None else None,
                       round(_es, 2) if _es is not None else None))
    # Hand the raw method segments to the dynamic-slot gate (DESIGN.md
    # section 6): the gate needs method-level evidence, not blocks.
    if candidates_out is not None:
        candidates_out.extend(candidates)
    # Weak-evidence label rule (2026-11-15, A/B test — NOT merged):
    # strip the sponsor name from weak blocks only. Spans and votes
    # are already final here; rescue/xep/slot flow downstream read
    # only whether a label part matches a description sponsor, and
    # 'ad' matches none, exactly as 'dry-island' matched none.
    scored = _strip_weak_labels(scored)
    # URL-stem guard (2026-11-15, A/B test — NOT merged): strip
    # the sponsor name from blocks whose URL-evidenced label
    # fuzzy-matches the show's own identity. Spans and votes are
    # already final here; rescue/xep/slot flow downstream read
    # only whether a label part matches a description sponsor,
    # and 'ad' matches none, exactly as the stripped stems did
    # ('friedman' / 'lexfreedman' / 'hubertmanlab' match none).
    scored = _strip_own_identity_labels(scored, sponsors, own_identity)
    return scored, suspects


# ---------------------------------------------------------------------------
# Cross-episode recurrence, POSITIVE direction only (2026-11-11, KEPT)
# Port of the SB-eval xep_corroborate variant
# (sb_eval/ab_detect_xep_corroborate.py::apply_xep_corroboration).
# A recurring span — 8-word shingles of this transcript that appear
# in >= 2 prior episodes of the same podcast — is sponsor copy
# repeated across episodes. When such a span overlaps a block the
# detector already found, it corroborates that block: the block's
# edges extend to the span's bounds, so a tail-only read (votes
# arrive only at the CTA/URL tail) recovers its story-opening
# seconds. It never creates a block, never adds a vote, never adds
# a mention, and never touches find_mentions / norm_words / sponsors
# — so the brand-orphan path cannot flip (the phonetic-redesign
# failure mode: extra mentions moving a sponsor from the orphan path
# to the brand path and shrinking its block).
# Guards, mirroring the orphan merge: a span must overlap the block
# by >= XEP_MIN_OVERLAP_S (a span that barely touches a block is weak
# corroboration); max 90s extension per side; start side stops at
# the first opening cue in between; end side stops at the first
# return-to-content cue; neither side crosses a different sponsor's
# mention or an adjacent block. The closest overlapping span bound
# is used (least extension). Sharpness is recomputed for moved edges.
# Measured on the 25-episode SB set: recall 0.673 -> 0.701, FP
# 36 -> 33, median 4.7 -> 4.0s, within-1s 26.4% -> 29.3%.
# Runs in process_episode AFTER detect_ad_blocks and BEFORE the
# dynamic-slot flow (the harness order): the slot flow can replace
# a block's edges with the more accurate slot edges, so xep fixes
# detector-only edges and slots override where they gate.
XEP_MAX_EXT = 90.0
XEP_MIN_OVERLAP_S = 5.0


def _xep_part(label):
    # Labels can be None: the slot flow legitimately emits unnamed
    # dynamic slots; a None label has no sponsor part (m12 — the same
    # guard split_rescue._part already has).
    if not label:
        return ""
    return label.split(":")[-1].strip().lower().replace(" ", "")


def _apply_xep_corroboration(blocks, words, sponsors, silences,
                             candidates, ep_dir, slug):
    """Extend already-detected blocks to overlapping recurring-span
    bounds (positive direction only). Returns the (possibly extended)
    block list, sorted, with sharpness recomputed for moved edges.
    Blocks, labels, and sources are otherwise untouched."""
    try:
        import xep_recurrence
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import xep_recurrence
    spans, _priors = xep_recurrence.recurring_spans_for_episode(
        ep_dir, slug)
    if not spans or not blocks:
        return blocks
    nw = norm_words(words)
    mentions = {}
    for sp in sponsors:
        hits = find_mentions(nw, sp["variants"])
        mentions[sp["label"].lower().replace(" ", "")] = sorted(
            words[h][0] for h in hits)
    out = []
    for bi, (bs, be, label, ss, es) in enumerate(blocks):
        part = _xep_part(label)
        cand = [s for s in spans
                if min(s["end"], be) - max(s["start"], bs) >= XEP_MIN_OVERLAP_S]
        ns, ne = bs, be
        starts = [s["start"] for s in cand if s["start"] < bs]
        if starts:
            t = max(starts)
            t = max(t, bs - XEP_MAX_EXT)
            cue = _first_opening_cue_after(words, nw, silences, t, bs)
            if cue is not None:
                t = max(t, cue)
            for (bs2, be2, *_r) in blocks:
                if be2 <= bs and be2 > t:
                    t = max(t, be2)
            for p, times in mentions.items():
                if p == part:
                    continue
                if any(t < x < bs for x in times):
                    t = bs
                    break
            ns = t
        ends = [s["end"] for s in cand if s["end"] > be]
        if ends:
            t = min(ends)
            t = min(t, be + XEP_MAX_EXT)
            for i in range(len(nw)):
                if not (be < words[i][0] < t):
                    continue
                if any(c in " ".join(nw[i:i + 4]) for c in RETURN_CUES):
                    t = min(t, words[i][0])
                    break
            for (bs2, be2, *_r) in blocks:
                if bs2 > be and bs2 < t:
                    t = min(t, bs2)
            for p, times in mentions.items():
                if p == part:
                    continue
                if any(be < x < t for x in times):
                    t = be
                    break
            ne = t
        if ne - ns > MAX_AD_LEN:
            # M25: an extension past MAX restores the pre-extension
            # edges — xep is corroboration only, and the original
            # block was already valid.
            log(f"  xep-corroborate '{label}': extension to "
                f"{ns:.0f}s-{ne:.0f}s exceeds MAX_AD_LEN; keeping "
                f"{bs:.0f}s-{be:.0f}s")
            ns, ne = bs, be
        if ns != bs or ne != be:
            log(f"  xep-corroborate '{label}': {bs:.0f}s-{be:.0f}s -> "
                f"{ns:.0f}s-{ne:.0f}s")
        out.append((ns, ne, label, ss, es))
    out.sort()
    # Recompute boundary sharpness for moved edges (same functions the
    # pipeline uses; None when a window is empty).
    votes = _word_votes(words, candidates)
    starts = [w[0] for w in words]
    ends = [w[1] for w in words]
    rescored = []
    for s, e, l, ss, es in out:
        _ss = _edge_sharpness(starts, ends, votes, s, "start")
        _es = _edge_sharpness(starts, ends, votes, e, "end")
        rescored.append((s, e, l,
                         round(_ss, 2) if _ss is not None else None,
                         round(_es, 2) if _es is not None else None))
    return rescored


def _expand_tight_cluster(words, nw, silences, hits):
    """Fallback for contact-anchored spots with no host cue and no
    produced-spot sign-off (Unsubscribe 286 Got Junk, 2026-10-26: the spot
    repeats "1-800-GOT-Junk" four times in 28s but has neither an opening
    cue nor a thanks/support sign-off, so _expand_segment gives up).

    A tight cluster of brand mentions around a URL/phone anchor is itself
    strong ad evidence. Requires >= 2 mentions spanning <= 90s, snapped to
    nearby pauses. Returns (start, end) or None.
    """
    if len(hits) < 2:
        return None
    times = [words[h][0] for h in hits]
    first_t, last_t = min(times), max(times)
    if last_t - first_t > 90:
        return None
    pauses = get_pauses(words, silences)
    seg_start = first_t - 10.0
    for ps, pe in pauses:
        if pe <= first_t and pe >= first_t - 20:
            seg_start = pe
    seg_end = last_t + 10.0
    for ps, pe in pauses:
        if ps >= last_t and ps <= last_t + 20:
            seg_end = ps
            break
    dur = seg_end - seg_start
    if not (15 <= dur <= 150):
        return None
    return seg_start, seg_end


def _evidence_sentence_trim(words, nw, s, e, variants):
    """Trim a CTA/URL segment to the sentences carrying its own brand
    evidence (2026-10-29). Plain sentence-trimming does not work here:
    a CTA segment that overshoots into content contains complete
    content sentences, so 'last sentence end' lands in content. The
    ad's closing sentence is the last sentence containing a brand
    mention or URL for that segment's brand; its opening sentence is
    the first such sentence. Start moves forward to the first evidence
    sentence's first word; end moves back to the last evidence
    sentence's last word. No evidence in the segment -> unchanged.
    Only shrinks, never expands.
    """
    idx = [i for i, w in enumerate(words) if s <= w[0] <= e]
    if not idx:
        return s, e
    hits = [h for h in find_mentions(nw, variants) if s <= words[h][0] <= e]
    if not hits:
        return s, e
    # Split idx into sentences (a sentence ends at . ! ?).
    bounds = []
    start = idx[0]
    for i in idx:
        txt = words[i][2].strip().rstrip('"\'')
        if txt.endswith(('.', '!', '?')):
            bounds.append((start, i))
            start = i + 1
    if start <= idx[-1]:
        bounds.append((start, idx[-1]))
    if not bounds:
        return s, e

    def sent_of(h):
        for a, b in bounds:
            if a <= h <= b:
                return a, b
        return None
    first = sent_of(hits[0])
    last = sent_of(hits[-1])
    if first is None or last is None:
        return s, e
    ns = words[first[0]][0]
    ne = words[last[1]][1]
    if ne <= ns:
        return s, e
    return ns, ne



def _method_history_preroll(words):
    """Method 'history-preroll': one heat-map vote on the window where
    this show's first ad usually starts (2026-11-14, A/B test).

    Corroborating vote only: the method is not in _single_methods, not
    in the suspect-method list, and its label is artifact-classed in
    _pick_label, so it can never mark a block alone or name one. The
    window comes from HISTORY_PREROLL_WINDOW, set by the harness from
    the leave-one-out history prior; None means the voter is silent
    (show below the 5-episode gate, or no slot-1 prior). The bridging
    guard in detect_ad_blocks punches holes in this segment where it
    would connect two different-label baseline regions.
    """
    if not HISTORY_PREROLL_WINDOW:
        return []
    ws, we = HISTORY_PREROLL_WINDOW
    if we - ws < 1:
        return []
    return [(ws, we, 'history-prior')]


def _method_cta(words, sponsors, silences, own_domains=None):
    """Method 'cta': Generic URL/promo-code CTA detection.
    Returns [(start, end, label)] independently."""
    nw = norm_words(words)
    results = []
    for brand, url_idx in find_url_mentions(nw, own_domains):
        brand_variants = {brand, brand.replace(" ", "")}
        hits = find_mentions(nw, brand_variants)
        url_t = words[url_idx][0]
        hits = [h for h in hits if abs(words[h][0] - url_t) < 8 * 60]
        if not hits:
            hits = [url_idx]
        seg = _expand_segment(words, nw, silences, hits, brand)
        if seg is None:
            seg = _expand_tight_cluster(words, nw, silences, hits)
        if seg:
            results.append((seg[0], seg[1], brand))
    return results


def _method_url(words, silences, own_domains=None):
    """Method 'url': spoken URLs WITHOUT a CTA phrase (Unsubscribe 287
    Leaf Filter, 2026-10-28: dynamic spots say 'at leaffilter.com slash
    gold' with no CTA phrase from cta_phrases, so find_url_mentions
    never anchors them, and the same gap left 286 Leaf Filter and
    286 Ozempic with only a reverb vote). A spoken domain is itself
    strong ad evidence. Low-confidence: heat-map votes only, never
    marks alone (like 'voice' and 'cluster-popular').

    Segment: URL positions for one brand within 60s form one group (a
    spot repeats its URL twice); the segment runs [first-45s, last+10s]
    snapped to pauses — ads put the URL at the end, so most of the
    spot precedes it. Returns [(start, end, label)] independently.
    """
    nw = norm_words(words)
    positions = find_bare_urls(nw, own_domains)
    if not positions:
        return []
    pauses = get_pauses(words, silences)
    # Group positions within 60s (a spot repeats its URL twice, and a
    # spelled-out URL yields fragment stems like 'filter' from
    # 'L-E-A-F filter .com' — the first brand in the group names it).
    groups = []
    for brand, idx in positions:
        t = words[idx][0]
        # Chained 60s gaps with no span cap let one extra URL push a
        # group over the 180s segment limit and drop the whole group
        # (M24). A group's segment is [first-45, last+10], so split
        # when the new URL would make first-to-last > 125s.
        if groups and t - groups[-1][-1][1] <= 60 and \
                t - groups[-1][0][1] <= 125:
            groups[-1].append((brand, t))
        else:
            groups.append([(brand, t)])
    results = []
    for g in groups:
        brand = g[0][0]
        start, end = g[0][1] - 45.0, g[-1][1] + 10.0
        # Snap to speech, not to silence (M23): the start moves to
        # the silence END (pe) and the end to the silence START (ps),
        # as _expand_tight_cluster already does.
        for ps, pe in pauses:
            if ps <= start < pe:
                # T2-N1: a raw edge strictly INSIDE a silence snaps to
                # the speech side (the silence end), as the adjacent
                # case below already does.
                start = pe
                break
            if ps >= start and ps - start <= 10.0:
                start = pe
                break
        for ps, pe in reversed(pauses):
            if ps < end <= pe:
                # T2-N1: a raw edge strictly INSIDE a silence snaps to
                # the speech side (the silence start).
                end = ps
                break
            if pe <= end and end - pe <= 10.0:
                end = ps
                break
        start = max(0.0, start)
        if not (20 <= end - start <= 180):
            continue
        results.append((start, end, brand))
    return results


def _method_brand(words, sponsors, silences):
    """Method 'brand': Description sponsor brand mentions.
    Returns [(start, end, label)] independently."""
    nw = norm_words(words)
    results = []
    for sp in sponsors:
        hits = find_mentions(nw, sp["variants"])
        if not hits:
            continue
        seg = _expand_segment(words, nw, silences, hits, sp["label"])
        if seg:
            results.append((seg[0], seg[1], sp["label"]))
    return results


def _method_brand_orphan(words, sponsors, silences):
    """Method 'brand-orphan': sponsor mentions _expand_segment could not
    turn into a block (no opening cue found). Low-confidence: votes in the
    heat map but never cuts alone (like 'voice' and 'cluster-popular').
    Added 2026-10-14 per the owner — Unsubscribe 268 showed DOSE/AG1 mentions
    being silently dropped this way. Window = the densest mention
    cluster's span padded 45s each side, snapped to pauses, bounded by
    MIN/MAX_AD_LEN. Mentions are split into clusters at gaps >
    ORPHAN_GAP_MAX (M11): the whole-episode span fused separate reads /
    content mentions into one near-episode-long window, but a
    densest-<=90s window cut genuine reads whose mentions span the
    whole read (SN-1096 lost 2 ads in SB), and one segment per cluster
    turned every scattered content mention into its own block (SB FP
    40 -> 59). A cluster is one read; the densest cluster is the read.
    Returns [(start, end, label)] independently."""
    nw = norm_words(words)
    pauses = get_pauses(words, silences)
    results = []
    for sp in sponsors:
        hits = find_mentions(nw, sp["variants"])
        if not hits:
            continue
        if _expand_segment(words, nw, silences, hits, sp["label"]):
            continue  # the full brand method already handles it
        times = sorted(words[i][0] for i in hits)
        # Split into clusters at gaps > ORPHAN_GAP_MAX (M11) and keep
        # only the DENSEST cluster (most mentions; ties -> earliest):
        # a genuine read mentions the brand throughout, so it is the
        # densest cluster and its window spans the whole read; far-apart
        # mentions are separate reads / content and are ignored. One
        # segment per sponsor — one segment per cluster (v2) turned
        # every scattered content mention into its own block
        # (SB FP 40 -> 59).
        clusters = [[times[0]]]
        for _t in times[1:]:
            if _t - clusters[-1][-1] > ORPHAN_GAP_MAX:
                clusters.append([_t])
            else:
                clusters[-1].append(_t)
        _cl = max(clusters, key=lambda c: (len(c), -c[0]))
        start, end = max(0.0, _cl[0] - 45.0), _cl[-1] + 45.0
        # snap to nearby pauses, never into neighboring content
        # Snap to speech (M23): start to the silence end, end to
        # the silence start.
        for ps, pe in pauses:
            if ps <= start < pe:
                # T2-N1: a raw edge strictly INSIDE a silence snaps to
                # the speech side (the silence end).
                start = pe
                break
            if ps >= start and ps - start <= 6.0:
                start = pe
                break
        for ps, pe in reversed(pauses):
            if ps < end <= pe:
                # T2-N1: a raw edge strictly INSIDE a silence snaps to
                # the speech side (the silence start).
                end = ps
                break
            if pe <= end and end - pe <= 6.0:
                end = ps
                break
        if not (MIN_AD_LEN <= end - start <= MAX_AD_LEN):
            continue
        results.append((start, end, f"brand-orphan:{sp['label']}"))
        log(f"  brand-orphan '{sp['label']}': {start:.1f}s-{end:.1f}s "
            f"({len(_cl)} mentions in cluster, no opening cue)")
    return results


def _method_cluster(words, sponsors, silences):
    """Method 'cluster': Dense brand-mention clusters (casual ad reads).
    Returns [(start, end, label)] independently.
    Threshold: 4+ mentions in 1 minute (tightened 2026-10-09 per the owner;
    3-in-3min was cutting wanted content)."""
    nw = norm_words(words)
    results = []
    for sp in sponsors:
        hits = find_mentions(nw, sp["variants"])
        if not hits:
            continue
        times = [words[h][0] for h in hits]
        best = None
        for i in range(len(times)):
            j = i
            while j < len(times) and times[j] - times[i] <= 60:
                j += 1
            count = j - i
            if count >= 4 and (best is None or count > best[0]):
                best = (count, times[i], times[j - 1])
        if best:
            count, cs, ce = best
            seg_start = find_pause_before(words, silences, cs)
            seg_end = find_pause_after(words, silences, ce)
            # Bound to the mention span +-60s (2026-10-28): pause
            # snapping in unbroken debate speech ballooned 287's Poncho
            # candidate to 2590-2932, bridging Leaf Filter #2 and Poncho
            # into one heat-map run that the cap then trimmed to Poncho
            # alone — eating a real ad.
            seg_start = max(seg_start, cs - 60)
            seg_end = min(seg_end, ce + 60)
            results.append((seg_start, seg_end, sp["label"]))
    return results


def _method_cluster_popular(words, sponsors, silences):
    """Method 'cluster-popular': Dense mentions of popular sponsors NOT in
    the episode description (safety net for unlisted/dynamic ads).
    Same 4+/1min threshold. Tagged separately so the heat map treats
    single-vote hits as low-confidence (not auto-cut).
    Returns [(start, end, label)] independently."""
    # Skip brands already in the description sponsors (they're covered
    # by _method_cluster with high confidence)
    desc_labels = {sp["label"].lower() for sp in sponsors}
    popular = [pb for pb in POPULAR_BRANDS
               if pb["label"].lower() not in desc_labels]
    nw = norm_words(words)
    results = []
    for pb in popular:
        hits = find_mentions(nw, pb["variants"])
        if not hits:
            continue
        times = [words[h][0] for h in hits]
        best = None
        for i in range(len(times)):
            j = i
            while j < len(times) and times[j] - times[i] <= 60:
                j += 1
            count = j - i
            if count >= 4 and (best is None or count > best[0]):
                best = (count, times[i], times[j - 1])
        if best:
            count, cs, ce = best
            seg_start = find_pause_before(words, silences, cs)
            seg_end = find_pause_after(words, silences, ce)
            # Same +-60s bound as _method_cluster (pause-snapping
            # balloons in unbroken speech).
            seg_start = max(seg_start, cs - 60)
            seg_end = min(seg_end, ce + 60)
            results.append((seg_start, seg_end, pb["label"]))
    return results


def _expand_segment(words, nw, silences, hits, label):
    """Expand hit indices to segment boundaries. Returns (start, end, kind) or None."""
    first, last = hits[0], hits[-1]
    seg_start = find_opening(words, nw, silences, first)
    if seg_start is None:
        produced = find_produced_spot(words, nw, hits, silences)
        if produced is None:
            return None
        if produced[1] - produced[0] > MAX_AD_LEN:
            log(f"  expand '{label}': produced spot "
                f"{produced[0]:.1f}s-{produced[1]:.1f}s exceeds "
                f"MAX_AD_LEN; dropped")
            return None
        return produced[0], produced[1], "produced spot"
    seg_end = find_closing(words, nw, silences, last, seg_start)
    if seg_end is None:
        return None
    if seg_end - seg_start > MAX_AD_LEN:
        log(f"  expand '{label}': {seg_start:.1f}s-{seg_end:.1f}s "
            f"exceeds MAX_AD_LEN; dropped (mentions span two reads?)")
        return None
    return seg_start, seg_end, "host read"


def _method_voice(words, mp3_path):
    """Method 'voice': Host-voice anomaly detection (proposed 2026-10-09).
    Learn host voice profile, flag segments far from it.
    Returns [(start, end, label)]."""
    try:
        import numpy as np
        from python_speech_features import mfcc
        from sklearn.preprocessing import StandardScaler
        import wave
    except ImportError:
        log("  voice: python_speech_features not available — skipping")
        return []

    # Extract audio
    import tempfile
    # Next to the mp3, not /tmp: a 2hr episode decodes to ~260MB and
    # /tmp is a 512MB tmpfs (2026-10-28: the voice decode failed with
    # /tmp full). Still deleted in the finally block below.
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir=os.path.dirname(mp3_path)) as tf:
        wav_path = tf.name
    try:
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', mp3_path,
                        '-ar', '16000', '-ac', '1', wav_path], check=True)
        w = wave.open(wav_path)
        frames = w.readframes(w.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(float)
        sr = w.getframerate()
        w.close()
    except Exception as e:
        # Voice is a low-confidence heat-map-only voter: a bad decode
        # must degrade to "method silent", not fail the episode (M20).
        log(f"  voice: audio decode failed ({type(e).__name__}: {e}) — skipping")
        return []
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)

    # 10s segments (more stable than 5s)
    seg_len = 10 * sr
    features = []
    for i in range(0, len(audio) - seg_len, seg_len):
        seg = audio[i:i+seg_len]
        m = mfcc(seg, samplerate=sr, numcep=13)
        features.append(m.mean(axis=0))
    if not features:
        return []
    features = np.array(features)

    # Host profile = median (robust)
    scaler = StandardScaler()
    fnorm = scaler.fit_transform(features)
    host_profile = np.median(fnorm, axis=0)
    dists = np.linalg.norm(fnorm - host_profile, axis=1)

    # Flag top 5% distance, require 15s+ contiguous
    threshold = np.percentile(dists, 95)
    results = []
    in_block = False
    bs = 0
    for i, d in enumerate(dists):
        t = i * 10
        if d > threshold and not in_block:
            in_block = True
            bs = t
        elif d <= threshold and in_block:
            in_block = False
            be = t
            if be - bs >= 15:
                results.append((bs, be, f"voice-anomaly"))
    if in_block:
        be = len(dists) * 10
        if be - bs >= 15:
            results.append((bs, be, f"voice-anomaly"))
    log(f"  voice: {len(results)} anomalous segments")
    return results


def _method_reverb(words, mp3_path):
    """Method 'reverb': room-echo dryness vote (proposed 2026-10-26).

    Ad segments on conversational shows are recorded drier than the
    episode's reverberant content. Feature: late autocorrelation of the
    20ms RMS envelope at 60-300ms lags — reverb smearing raises it.
    Unsubscribe 286: ads average 0.212 vs content 0.367.

    GATED: only runs when the episode's content is itself reverberant
    (median lateAC >= 0.25). On dry professional shows (Security Now 1101
    median 0.172) the method is meaningless and stays silent. Threshold is
    adaptive (episode median - 0.08), never absolute.

    Low-confidence vote only — blind dryness scanning finds 40+ dry
    content patches on 286, so dryness alone must never cut.
    Returns [(start, end, label)].
    """
    try:
        import numpy as np
        import wave
    except ImportError:
        return []
    import tempfile
    # Next to the mp3, not /tmp: a 2hr episode decodes to ~260MB and
    # /tmp is a 512MB tmpfs (2026-10-28: the voice decode failed with
    # /tmp full). Still deleted in the finally block below.
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir=os.path.dirname(mp3_path)) as tf:
        wav_path = tf.name
    try:
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', mp3_path,
                        '-ar', '16000', '-ac', '1', wav_path], check=True)
        w = wave.open(wav_path)
        sr = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()),
                              dtype=np.int16).astype(float) / 32768.0
        w.close()
    except Exception as e:
        # As _method_voice (M20): reverb is heat-map-only; a bad
        # decode degrades to silent, it must not fail the episode.
        log(f"  reverb: audio decode failed ({type(e).__name__}: {e}) — skipping")
        return []
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
    FL = int(0.02 * sr)
    nfr = len(audio) // FL
    if nfr < 1000:  # under 20s of audio: no meaningful windows
        return []
    env = np.sqrt(np.mean(audio[:nfr * FL].reshape(nfr, FL) ** 2, axis=1))
    # 20s windows, 5s hop (50 frames/s)
    W, H = 20 * 50, 5 * 50
    series = []
    for start in range(0, len(env) - W, H):
        e = env[start:start + W]
        ec = e - np.mean(e)
        denom = np.sum(ec ** 2) + 1e-12
        acs = [np.sum(ec[:-lag] * ec[lag:]) / denom
               for lag in (3, 5, 8, 12, 15)]
        series.append((start / 50, float(np.mean(acs))))
    if len(series) < 5:
        return []
    med = float(np.median([v for _, v in series]))
    if med < 0.25:
        log(f"  reverb: episode median lateAC {med:.3f} — dry show, "
            "method disabled")
        return []
    th = med - 0.08
    flagged = [t for t, v in series if v < th]
    segs = []
    for t in flagged:
        if segs and t - segs[-1][1] <= 5:
            segs[-1][1] = t + 20
        else:
            segs.append([t, t + 20])
    out = []
    for s, e in segs:
        if e - s >= 25:
            out.append((s, e, "dry-island"))
            log(f"  reverb dry island {s:.0f}s-{e:.0f}s ({e-s:.0f}s, "
                f"median {med:.3f})")
    return out


def find_duplicate_segments(words, min_shingle=8, min_gap=60, min_dur=15):
    """Find verbatim-repeated segments (inserted ads run 2+ times).
    Returns [(start, end, label)]."""
    from collections import defaultdict
    nw = norm_words(words)
    # Build shingles: 8 normalized words -> list of timestamps
    shingles = defaultdict(list)
    n = len(nw)
    for i in range(n - min_shingle + 1):
        key = tuple(nw[i + k] for k in range(min_shingle))
        if all(key):
            shingles[key].append(words[i][0])
    # Find shingles repeated with >min_gap separation
    hits = []  # (time,)
    for key, times in shingles.items():
        if len(times) >= 2 and max(times) - min(times) > min_gap:
            hits.extend(times)
    if not hits:
        return []
    # Cluster hit times into segments (within 40s = same ad)
    hits.sort()
    clusters = []
    cs, ce = hits[0], hits[0]
    for t in hits[1:]:
        if t - ce < 40:
            ce = t
        else:
            clusters.append((cs, ce))
            cs, ce = t, t
    clusters.append((cs, ce))
    # Expand each cluster to cover the full repeated phrase (+15s each side
    # for the ad boundaries) and label
    result = []
    for i, (cs, ce) in enumerate(clusters):
        dur = ce - cs
        if dur < 5:  # too short, likely coincidence
            continue
        # Expand by 20s each side to catch full ad
        ds = max(0, cs - 20)
        de = ce + 20
        if de - ds >= min_dur:
            result.append((ds, de, f"duplicate-{i+1}"))
    return result


def find_pause_before(words, silences, t):
    """Nearest silence start before time t (for clean cut boundaries)."""
    best = None
    for ss, se in silences:
        if se <= t and (best is None or se > best):
            best = se
    # fall back to word boundary 2s before
    if best is None:
        best = max(0, t - 2)
    return best


def find_pause_after(words, silences, t):
    """Nearest silence end after time t (for clean cut boundaries)."""
    best = None
    for ss, se in silences:
        if ss >= t and (best is None or ss < best):
            best = ss
    if best is None:
        best = t + 2
    return best


def detect_narrative_breaks(words, silences):
    """Find ad blocks via SEMANTIC topical discontinuity.

    Uses sentence embeddings to detect "jarring narrative jumps":
    window A (topic X) -> window B (unrelated) -> window C (topic X again).
    Returns [(start, end, label)].
    """
    import os
    # workaround: httpx misparses the no_proxy var's IPv6 brackets
    os.environ.pop('no_proxy', None)
    os.environ.pop('NO_PROXY', None)

    try:
        from sentence_transformers import SentenceTransformer
        import numpy as np
    except ImportError:
        log("  narrative: sentence-transformers not available — skipping")
        return []

    WIN = 30
    if not words:
        return []
    total = words[-1][0] + 2
    nwin = int(total / WIN) + 1

    # build window texts
    win_texts = [""] * nwin
    for w in words:
        idx = int(w[0] / WIN)
        if idx < nwin:
            win_texts[idx] += w[2] + " "
    win_texts = [t.strip() for t in win_texts]
    # filter empty
    valid = [i for i, t in enumerate(win_texts) if len(t.split()) >= 10]
    if len(valid) < 3:
        return []

    log("  narrative: loading embedding model...")
    model = SentenceTransformer('all-MiniLM-L6-v2')
    texts = [win_texts[i] for i in valid]
    embs = model.encode(texts, show_progress_bar=False)
    # cosine similarity matrix
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    embs_n = embs / norms
    sim = embs_n @ embs_n.T

    # map valid-index -> position in sim matrix
    blocks = []
    for k in range(1, len(valid) - 1):
        i_prev, i_cur, i_next = valid[k-1], valid[k], valid[k+1]
        # require consecutive windows (no gaps)
        if not (i_cur == i_prev + 1 and i_next == i_cur + 1):
            continue
        sab = sim[k-1, k]
        sbc = sim[k, k+1]
        # B semantically distant from BOTH neighbors.
        # (The "return" pattern doesn't hold in meandering conversation;
        #  the ad is simply a semantic island.)
        if sab < 0.35 and sbc < 0.35:
            start = i_prev * WIN
            # extend while disconnected
            j = k
            while j + 1 < len(valid) - 1:
                jk, jk1 = valid[j], valid[j+1]
                if jk1 != jk + 1:
                    break
                if sim[j, j+1] < 0.40:
                    j += 1
                else:
                    break
            end = (valid[j] + 1) * WIN
            dur = end - start
            if 45 <= dur <= 720:
                ps = find_pause_before(words, silences, start + 5)
                pe = find_pause_after(words, silences, end - 5)
                blocks.append((ps, pe, f"semantic-break@{int(start)}s"))
                log(f"  narrative: {ps:.0f}s-{pe:.0f}s "
                    f"(sim in/out {sab:.2f}/{sbc:.2f})")
    return blocks


def _is_ad_chapter_title(title):
    """Does a chapter title label an ad segment?"""
    t = title.strip()
    return any(re.search(p, t, re.I) for p in CHAPTER_AD_PATTERNS)


def _merge_chapters(id3_chapters, rss_chapters):
    """Combine ID3 and RSS chapter lists, deduping near-identical starts
    so one ad chapter doesn't vote twice."""
    merged = list(id3_chapters)
    for sec, title in rss_chapters:
        # Dedupe by time AND title class (M22): an RSS ad chapter
        # starting within 2s of a non-ad chapter is kept — dropping it
        # loses a high-precision chapter vote. Drop the RSS entry only
        # when it is not an ad title, or the near chapter is already
        # an ad title.
        near_any = any(abs(sec - s) < 2.0 for s, _ in merged)
        near_ad = any(abs(sec - s) < 2.0 and _is_ad_chapter_title(t)
                      for s, t in merged)
        if near_any and (not _is_ad_chapter_title(title) or near_ad):
            continue
        merged.append((sec, title))
    return sorted(merged)


def _method_chapter(words, chapters, silences, audio_duration=None):
    """Method 'chapter': publisher-labeled ad chapters (2026-10-14).

    Chapters titled like '[Ad]', 'Ad:', 'Advertisement', 'Sponsor' are
    high-precision ad markers. Span = chapter start -> next chapter start
    (or episode end), snapped to nearby pauses for clean chapter edges.
    Returns [(start, end, label)] independently.
    """
    if not chapters:
        return []
    if not words:
        return []
    # Last word's END, not its start: the final chapter runs to the
    # end of the last transcribed word (M21). T2-N7: when the audio
    # duration is known, the final chapter runs to the audio end — a
    # publisher chapter with no successor runs to the episode end.
    duration = max(words[-1][1], audio_duration) \
        if audio_duration else words[-1][1]
    pauses = get_pauses(words, silences)
    results = []
    ordered = sorted(chapters)
    for i, (start, title) in enumerate(ordered):
        if not _is_ad_chapter_title(title):
            continue
        end = ordered[i + 1][0] if i + 1 < len(ordered) else duration
        # Snap to nearby pauses for clean cuts, never into neighboring
        # content: start snaps forward, end snaps backward (<=4s each).
        # Snap to speech (M23): start to the silence end, end to
        # the silence start.
        for ps, pe in pauses:
            if ps <= start < pe:
                # T2-N1: a raw edge strictly INSIDE a silence snaps to
                # the speech side (the silence end).
                start = pe
                break
            if ps >= start and ps - start <= 4.0:
                start = pe
                break
        for ps, pe in reversed(pauses):
            if ps < end <= pe:
                # T2-N1: a raw edge strictly INSIDE a silence snaps to
                # the speech side (the silence start).
                end = ps
                break
            if pe <= end and end - pe <= 4.0:
                end = ps
                break
        if end - start < 5:
            continue
        results.append((start, end, f"chapter:{title.strip()[:40]}"))
        log(f"  chapter marker '{title.strip()[:40]}': {start:.1f}s-{end:.1f}s")
    return results


def find_url_positions(nw):
    """All spoken URL positions: {word_index: brand stem}.

    Shared by find_url_mentions (CTA-gated anchors) and _method_url
    (bare URLs). TLDs come from the single shared URL_TLDS set (M15):
    a missing TLD is a silent miss: SN 1101 (2026-10-22) — the spoken
    "canary.tools" URL produced no URL anchor because "tools" was not
    in this set.
    """
    tlds = URL_TLDS  # single shared set (M15)
    L = len(nw)
    url_at = {}
    for i in range(L):
        w = nw[i]
        brand = None
        if w == "dot" and i + 1 < L and nw[i + 1].rstrip(".,") in tlds:
            parts = []
            for j in (i - 1, i - 2):
                if j < 0:
                    break
                pw = nw[j].strip(".,")
                if not re.fullmatch(r"[a-z0-9'-]+", pw) or pw in (
                        "to", "at", "go", "visit", "our", "my", "the", "a"):
                    break
                parts.append(pw)
            parts.reverse()
            if parts:
                brand = "".join(parts)
        elif w.startswith(".") and w[1:].rstrip(".,") in tlds and i > 0:
            pw = nw[i - 1].strip(".,")
            if re.fullmatch(r"[a-z0-9'-]{2,}", pw) and pw not in (
                    "our", "my", "the", "website", "site"):
                brand = pw
        if brand and brand not in ("website", "site", "page"):
            url_at[i] = brand
    return url_at


def find_bare_urls(nw, own_domains=None):
    """Every spoken URL as [(brand, word_index)], with NO CTA-phrase
    gate (contrast find_url_mentions). Used by _method_url and by the
    single-cluster anchor check. With own_domains (imp5), URLs on the
    show's own domain are excluded: content, not ad evidence."""
    url_at = find_url_positions(nw)
    out = [(url_at[i], i) for i in sorted(url_at)]
    if own_domains:
        out = [(b, i) for b, i in out if b.replace(" ", "") not in own_domains]
    return out


def find_url_mentions(nw, own_domains=None):
    """Find ad-like URL/CTA mentions: [(brand, word_index)].

    An ad anchor requires BOTH a call-to-action phrase AND a nearby URL.
    This filters out URLs mentioned in normal content. With own_domains
    (imp5), URLs on the show's own domain are excluded (same reason).
    Promo-code and toll-free anchors are not URLs and are kept.
    """
    cta_phrases = [
        ("go", "to"), ("visit",), ("check", "out"), ("head", "to"),
        ("head", "over", "to"), ("give", "it", "a", "try", "at"),
        ("try", "at"), ("sign", "up", "at"), ("get", "it", "at"),
        ("available", "at"),
    ]
    L = len(nw)
    url_at = find_url_positions(nw)
    out = []
    # promo codes are standalone CTAs
    for i in range(L):
        if nw[i] == "code" and i + 1 < L:
            if (i >= 1 and nw[i - 1] in ("use", "promo")) or \
               (i >= 2 and nw[i - 2] == "promo"):
                out.append((f"code-{nw[i + 1]}", i + 1))
    # CTA + nearby URL = ad anchor
    for i in range(L):
        for phrase in cta_phrases:
            n = len(phrase)
            if i + n < L and tuple(nw[i:i + n]) == phrase:
                for j in range(i + n, min(i + n + 15, L)):
                    if j in url_at:
                        out.append((url_at[j], j))
                        break
                break
    # Toll-free phone numbers are standalone CTAs like URLs (Unsubscribe
    # 286, 2026-10-26: the Got Junk spot has no URL, only "1-800-GOT-Junk").
    # Whisper keeps dashes attached: tokens "1", "-800", "-got", "-junk".
    # Strict pattern (leading "1" + toll-free code) so a bare number in
    # content ("800 people") does not fire.
    toll_free = {"800", "888", "877", "866", "855", "844", "833"}
    for i in range(L - 1):
        if nw[i] != "1" or nw[i + 1].lstrip("-") not in toll_free:
            continue
        parts = []
        for j in (i + 2, i + 3):
            if j >= L:
                break
            pw = nw[j].strip("-.,")
            if not re.fullmatch(r"[a-z][a-z0-9']*", pw):
                break
            parts.append(pw)
        if parts:
            out.append((" ".join(parts), i + 2))
    # de-dupe by brand, keep earliest index
    seen = {}
    for brand, idx in out:
        if brand not in seen:
            seen[brand] = idx
    out = [(b, seen[b]) for b in seen]
    if own_domains:
        # Own-domain URL anchors are content, not ads (imp5); promo-code
        # ('code-x') and toll-free anchors are not URLs and are kept.
        out = [(b, i) for b, i in out
               if b.startswith("code-") or b.replace(" ", "") not in own_domains]
    return out


def sponsor_root(label):
    """Distinctive word from a sponsor label, e.g. 'material.security' -> 'material'."""
    t = re.sub(r"^https?://(www\.)?", "", label.lower()).split("/")[0]
    t = re.sub(URL_TLD_RE, "", t)
    return t.split()[0] if t else ""


def check_missed_sponsors(words, sponsors, blocks):
    """Sponsors from the description with no marked block. Returns
    [{label, mentions}] so nothing is silently skipped.

    A sponsor counts as covered when any of its transcript mentions
    (fuzzy-matched, same as detection) falls inside a marked block, or a
    block label names it. Only genuinely unmarked mentions are reported —
    2026-10-12: exact-label matching falsely reported Chime/Quince as
    missed on Lore Legends 90 when their reads were inside a merged block.
    """
    nw = norm_words(words)
    missed = []

    def _label_part(lbl):
        # Sponsor part of a block label only, normalised exactly as
        # _pick_label / split_rescue._part do (M14): a substring test
        # on the whole label lets "cta:AG10" cover sponsor "AG1".
        return (lbl or "").split(":")[-1].strip().lower().replace(" ", "")

    for sp in sponsors:
        # No length filter (M13): short variants are already handled
        # by _boundary_match in detection, and filtering them here
        # silently skips short-named sponsors (FUM) entirely. Fall
        # back to the label itself when no variants exist at all.
        variants = {v for v in sp.get("variants", set()) if v}
        if not variants:
            variants = {sp["label"].lower()}
            log(f"  sponsor '{sp['label']}' has no variants; "
                "checking the label itself")
        hits = find_mentions(nw, variants)
        if not hits:
            # A block naming this sponsor covers it even with zero
            # transcript mentions (2026-11-15: FUM on Unsubscribe
            # 289 — Whisper heard "fumes", 0 mentions, yet a block
            # named FUM; Rorra on Huberman, heard "Rora"). The
            # block-label test below already exists for the hits>0
            # path; the zero-hit branch returned before reaching it.
            if any(_label_part(lbl) == sp["label"].lower().replace(" ", "")
                   for _, _, lbl, *_ in blocks):
                continue
            log(f"  WARNING: sponsor '{sp['label']}' not in audio (0 transcript mentions)")
            missed.append({"label": sp["label"], "mentions": 0})
            continue
        sp_part = sp["label"].lower().replace(" ", "")
        hit_times = [words[i][0] for i in hits]
        # Time coverage is label-aware (M14): a mention inside a block
        # labelled for a DIFFERENT sponsor (AG1 inside a cta:AG10
        # block) does not cover this sponsor — the audit's label test
        # alone would not fix R25, because the time test fires first.
        # Unnamed blocks (label None/"") can still cover by time:
        # they may be this sponsor's ad without a name. Trade-off
        # with the 2026-10-12 Lore case (a read inside a
        # differently-labelled merged block now reports missed): that
        # surfaces for operator review instead of silently assuming
        # coverage; a missed report is cheap, a silently unmarked ad
        # is not. T2-N6: a block whose part is GENERIC (names no
        # advertiser — 'ad' from the weak/own-identity strips,
        # 'dry-island', 'voice-anomaly', 'history-prior',
        # 'duplicate-N') is likewise unnamed for coverage: the ad IS
        # marked, only its name was stripped.
        covered = any(s <= t <= e for t in hit_times
                      for s, e, lbl, *_ in blocks
                      if _label_part(lbl) in ("", sp_part)
                      or _is_generic_part(_label_part(lbl)))
        if not covered:
            covered = any(_label_part(lbl) == sp_part
                          for _, _, lbl, *_ in blocks)
        if not covered:
            log(f"  WARNING: sponsor '{sp['label']}' not cut "
                f"({len(hits)} transcript mentions)")
            missed.append({"label": sp["label"], "mentions": len(hits)})
    return missed


def read_chapters(mp3_path):
    """Existing ID3 chapter markers. Returns [(start_ms, title)]."""
    from mutagen.mp3 import MP3
    try:
        audio = MP3(mp3_path)
    except Exception:
        return []
    if not audio.tags:
        return []
    chaps = []
    for key in audio.tags:
        if key.startswith("CHAP:"):
            c = audio.tags[key]
            title = ""
            if "TIT2" in c.sub_frames:
                title = str(c.sub_frames["TIT2"].text[0])
            chaps.append((c.start_time, title))
    return sorted(chaps)


def write_chapters(mp3_path, chapters, duration_s):
    """Embed chapter markers. chapters: [(start_ms, title)]."""
    from mutagen.mp3 import MP3
    from mutagen.id3 import CHAP, CTOC, TIT2
    audio = MP3(mp3_path)
    if audio.tags is None:
        audio.add_tags()
    # remove any existing chapters
    for key in [k for k in audio.tags if k.startswith("CHAP:") or k.startswith("CTOC:")]:
        del audio.tags[key]
    ids = []
    for i, (start_ms, title) in enumerate(chapters):
        eid = f"ch{i:02d}"
        end_ms = chapters[i + 1][0] if i + 1 < len(chapters) else int(duration_s * 1000)
        audio.tags.add(CHAP(element_id=eid, start_time=start_ms, end_time=end_ms,
                            start_offset=0, end_offset=0,
                            sub_frames=[TIT2(encoding=3, text=title)]))
        ids.append(eid)
    if ids:
        audio.tags.add(CTOC(element_id="toc", flags=3, child_element_ids=ids,
                            sub_frames=[TIT2(encoding=3, text="Chapters")]))
    audio.save()
    log(f"  wrote {len(ids)} chapters to {os.path.basename(mp3_path)}")


def generate_chapters(words, duration):
    """Auto-generate chapters from topic changes in the transcript.

    Splits at the largest pauses, targeting ~10 minute segments, and
    titles each from its most distinctive words. Returns [(start_ms, title)].
    """
    if not words or duration < 600:
        return []
    # candidate boundaries: pauses between words, largest first
    pauses = []
    for i in range(1, len(words)):
        gap = words[i][0] - words[i - 1][1]
        if gap >= 1.5:
            pauses.append((gap, words[i][0]))
    pauses.sort(reverse=True)
    # target ~1 chapter per 10 min, min 3, max 15
    n_target = max(3, min(15, int(duration / 600)))
    bounds = sorted({0.0} | {t for _, t in pauses[:n_target - 1]} | {duration})
    # merge segments shorter than 3 min into the next
    merged = [bounds[0]]
    for b in bounds[1:]:
        if b - merged[-1] < 180 and b != duration:
            continue
        merged.append(b)
    if len(merged) < 2:
        return []
    # word frequencies for the whole episode (for distinctiveness)
    stop = set("""the a an and or of to in on for with is are was were be been
        it its this that these those i you he she we they them his her our your
        as at by from but not no yes so if then than when where what which who
        whom whose how all any can could would should will shall may might must
        just very really much more most many some such only even still also
        out up down over under again once here there now today tonight well oh
        uh um yeah okay ok right sure like get got gets getting going go goes
        went come comes came know knows knew think thinks thought see sees saw
        look looks looked say says said tell tells told talk talks talked ask
        asked want wants wanted need needs needed take takes took make makes
        made use uses used do does did done have has had having thing things
        something anything everything nothing someone anyone everyone people
        one ones two three first second new old big small large little great
        good bad well better best much many lot lots kind sort type way ways
        part parts bit time times year years day days week weeks month months
        today tonight now then here there where when why how what which who
        don doesn didn isn aren wasn weren hasn haven hadn won wouldn couldn
        shouldn ll ve re m s t""".split())

    def clean_word(w):
        t = re.sub(r"[^a-z']", "", w.lower())
        # strip contractions: that's -> that, we're -> we, don't -> don
        t = re.sub(r"'(s|re|t|ll|ve|d|m)$", "", t)
        t = t.strip("'")
        return t

    total = {}
    seg_words = []
    for i in range(len(merged) - 1):
        s, e = merged[i], merged[i + 1]
        toks = [clean_word(w[2]) for w in words if s <= w[0] < e]
        toks = [t for t in toks if t and t not in stop and len(t) >= 4]
        seg_words.append(toks)
        for t in set(toks):
            total[t] = total.get(t, 0) + 1
    chapters = []
    for i, toks in enumerate(seg_words):
        if not toks:
            chapters.append((int(merged[i] * 1000), f"Part {i + 1}"))
            continue
        # Title from the most frequent content-word bigram — far more
        # topical than single words ("blind signature" vs "signature").
        from collections import Counter
        bigrams = Counter()
        for j in range(len(toks) - 1):
            a, b = toks[j], toks[j + 1]
            # skip stutters and short generic words
            if a != b and len(a) >= 5 and len(b) >= 5:
                bigrams[a + " " + b] += 1
        title = f"Part {i + 1}"
        if bigrams:
            bg, count = bigrams.most_common(1)[0]
            if count >= 3:
                title = " ".join(w.capitalize() for w in bg.split())
        chapters.append((int(merged[i] * 1000), title))
    return chapters


def build_segment_reel(mp3_path, blocks, out_path, work_dir):
    """Concatenate segments in chronological order into a reel with one
    chapter per segment. Used for suspect reels (2026-10-22, per the owner):
    uncertain segments for owner review instead of the whole episode."""
    if not blocks:
        return
    seg_files = []
    sorted_blocks = sorted(blocks)
    for i, (s, e, label) in enumerate(sorted_blocks):
        seg = os.path.join(work_dir, f"adseg_{i:02d}.mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", mp3_path,
             "-ss", f"{s:.2f}", "-to", f"{e:.2f}",
             "-c:a", "libmp3lame", "-b:a", "128k", seg], check=True)
        seg_files.append(seg)
    # Concatenate
    concat_list = os.path.join(work_dir, "ads_concat.txt")
    with open(concat_list, "w") as f:
        for seg in seg_files:
            f.write(f"file '{seg}'\n")
    tmp_out = os.path.join(work_dir, "reel_tmp.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", concat_list, "-c", "copy", tmp_out], check=True)
    # Add chapters, one per segment
    chap_time = 0
    meta = ";FFMETADATA1\n"
    for s, e, label in sorted_blocks:
        dur = e - s
        start_ms = int(chap_time * 1000)
        chap_time += dur
        end_ms = int(chap_time * 1000)
        # Sanitize label for metadata
        safe_label = re.sub(r"[^\w\s\-()]", "", str(label))[:80]
        meta += f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start_ms}\nEND={end_ms}\ntitle={safe_label}\n"
    meta_path = os.path.join(work_dir, "reel_chapters.txt")
    with open(meta_path, "w") as f:
        f.write(meta)
    # Atomic final write (resumable runs, 2026-11-21): same temp +
    # replace as _write_marked_file, so a kill can never leave a
    # partial reel at the final path.
    _final_tmp = _tmp_path(out_path)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", tmp_out, "-i", meta_path,
         "-map_metadata", "1", "-c", "copy", _final_tmp], check=True)
    _fsync_file(_final_tmp)
    os.replace(_final_tmp, out_path)
    _fsync_dir(os.path.dirname(out_path) or ".")
    for sf in seg_files + [tmp_out, concat_list, meta_path]:
        if os.path.exists(sf):
            os.remove(sf)


# Generic publisher chapter titles that name no sponsor -- these become a
# plain "Muse Detected Ad" chapter rather than "Muse Detected Ad For ...".
GENERIC_AD_TITLES = {
    "ad", "ads", "advertisement", "advertisements", "sponsor", "sponsors",
    "sponsored", "commercial", "commercials", "adbreak", "ad break",
    "promo", "promotion", "break",
}


def ad_chapter_name(label, sponsors):
    """Display name for a detected ad block's chapter, or None when the
    block names no sponsor (duplicate segments, generic publisher ad
    chapters). The candidate comes from the block label (heat-map labels
    are 'methods:sponsor'; CTA labels are 'cta:brand'; chapter labels are
    'chapter:<publisher title>'). Matched against description/house
    sponsors so Whisper-mangled brands resolve to the real company
    (threadlocker -> ThreatLocker); unmatched candidates are used as
    spoken (hawksont stays Hawksont)."""
    if not label:
        return None
    cand = label.split(":")[-1].strip()
    if not cand or re.fullmatch(r"duplicate-\d+", cand, re.I):
        return None
    norm = re.sub(r"[^a-z0-9 ]", "", cand.lower()).strip()
    if norm in GENERIC_AD_TITLES:
        return None

    def clean(name):
        name = re.sub(r"^https?://", "", name.strip()).split("/")[0]
        if "." in name:
            name = name.split(".")[0]
        name = name.strip()
        if name.isupper() or name.islower():
            name = name.title()
        return name

    low = cand.lower()
    for sp in sponsors:
        lab = sp.get("label", "")
        if lab.lower() == low or clean(lab).lower() == low:
            return clean(lab)
    # Fuzzy fallback for Whisper-mangled CTA brands.
    best, best_ratio = None, 0.0
    for sp in sponsors:
        c = clean(sp.get("label", ""))
        if not c:
            continue
        r = difflib.SequenceMatcher(None, low, c.lower()).ratio()
        if r > best_ratio:
            best, best_ratio = c, r
    if best and best_ratio >= 0.75:
        return best
    return clean(cand)


# House plugs (2026-11-06, per the owner): plugs for a show's own
# platforms/memberships (Pepperbox, Club TWiT) are a subtype of
# advertisement -- detected and marked like ads, but their chapter is
# titled "Muse Detected House Plug For <name>", never as an ad.
def is_house_plug(name, house_plugs):
    """True when a resolved chapter name is a configured house plug."""
    if not name or not house_plugs:
        return False
    low = name.strip().lower()
    return any(hp.get("label", "").lower() == low for hp in house_plugs)


def _ad_chapter_spans(blocks, sponsors, house_plugs=None):
    """Detected blocks -> chapter spans [(start_s, end_s, title)].
    Overlapping spans merge (first title wins); titles are
    'Muse Detected Ad For <name>' or plain 'Muse Detected Ad', except
    house plugs, which are titled 'Muse Detected House Plug For <name>'."""
    spans = []
    for s, e, label, *_ in sorted(blocks):
        name = ad_chapter_name(label, sponsors)
        if name and is_house_plug(name, house_plugs):
            title = f"Muse Detected House Plug For {name}"
        else:
            title = f"Muse Detected Ad For {name}" if name else "Muse Detected Ad"
        spans.append((float(s), float(e), title))
    merged = []
    for s, e, t in spans:
        if merged and s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), merged[-1][2])
        else:
            merged.append((s, e, t))
    return merged


def insert_ad_chapters(base_chapters, ad_spans, duration):
    """Merge ad chapters into a base chapter list, splitting base chapters
    at ad boundaries. base_chapters: [(start_s, title)] sorted.
    ad_spans: [(start_s, end_s, title)] sorted, non-overlapping.
    Returns [(start_ms, title)] for write_chapters."""
    points = {0.0, float(duration)}
    for s, _ in base_chapters:
        points.add(float(s))
    for s, e, _ in ad_spans:
        points.add(float(s))
        points.add(float(e))
    pts = sorted(p for p in points if 0.0 <= p <= duration)

    def ad_at(t):
        for s, e, title in ad_spans:
            if s <= t < e:
                return title
        return None

    def base_at(t):
        title = None
        for s, title0 in base_chapters:
            if s <= t:
                title = title0
            else:
                break
        return title

    out = []
    prev_kept_end = None
    for a, b in zip(pts, pts[1:]):
        if b - a < 0.5:
            continue
        title = ad_at(a) or base_at(a) or (base_chapters[0][1] if base_chapters else None)
        if title is None:
            continue
        # Merge only CONTIGUOUS same-title intervals (e.g. a base boundary
        # inside one ad). Two same-titled ads separated by content must
        # stay separate chapters -- merging them would swallow the content
        # between them into one long ad chapter. Contiguity is measured
        # from the last KEPT chapter's end (m11): the old check compared
        # against the previous interval's end, which is set even for
        # dropped (<0.5s) intervals — so two same-titled ads separated
        # by a dropped sliver of content merged into one chapter.
        if out and out[-1][1] == title and prev_kept_end == a:
            prev_kept_end = b
            continue
        out.append((a, title))
        prev_kept_end = b
    return [(int(s * 1000), t) for s, t in out]


def format_duration(seconds):
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def marked_summary_text(ep):
    """Plain-text summary prepended to feed descriptions (2026-10-28,
    chapters model): lists the ads marked as chapters. Podcast apps
    expect plain text descriptions -- no HTML."""
    blocks = ep.get("blocks", [])
    def _is_plug(b):
        return (b.get("chapter") or "").startswith("Muse Detected House Plug")
    ads = [b for b in blocks if not _is_plug(b)]
    plugs = [b for b in blocks if _is_plug(b)]
    total_ads = sum(b["seconds"] for b in ads)
    total_plugs = sum(b["seconds"] for b in plugs)
    lines = [
        "———————————————————",
        "",
    ]
    if ads:
        lines.append(
            f"Detected ads marked as chapters: {len(ads)} ad{'s' if len(ads) != 1 else ''} "
            f"totaling {total_ads / 60:.1f} minutes. Each ad plays in full -- "
            f"skip to the next chapter to pass it.")
        lines.append("")
    if plugs:
        lines.append(
            f"House plugs marked as chapters: {len(plugs)} plug{'s' if len(plugs) != 1 else ''} "
            f"totaling {total_plugs / 60:.1f} minutes.")
        lines.append("")
    if not blocks:
        lines.append(
            "Detected ads marked as chapters: 0 ads totaling 0.0 minutes. "
            "Each ad plays in full -- skip to the next chapter to pass it.")
        lines.append("")
    for b in blocks:
        title = b.get("chapter") or b.get("sponsor", "")
        short = title.replace("Muse Detected Ad For ", "").replace(
            "Muse Detected House Plug For ", "")
        start = b.get("start")
        if start is not None:
            m, s = divmod(int(start), 60)
            lines.append(f"- {short}: starts {m}:{s:02d} ({b['seconds'] / 60:.1f} min)")
        else:
            lines.append(f"- {short}: {b['seconds'] / 60:.1f} min")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def write_feed(state, site_dir, original_xml):
    """Mirror the original feed: identical channel metadata, artwork,
    titles and descriptions -- except marked episodes get the marked
    enclosure URL and a detected-ads summary prepended to the description.
    Episodes marked skipped (e.g. reverted 2026-10-28 clean slate) pass
    through untouched, so their enclosures point at the original
    publisher audio -- UNLESS they carry a pinned original (2026-10-31):
    a single fixed download uploaded as a release asset, swapped in as
    the enclosure with NO description change and NO ad chapters, so the
    owner listening to the feed hears exactly the file held locally for
    later manual ad-marking (dynamic publisher ads differ per download).
    When such an episode is later marked, clear its pinned_original_*
    fields and let the marked path take over. No bonus items are emitted:
    QA reels and unedited audit items were removed 2026-10-28 (suspect
    reels are release assets only, never feed items)."""
    base = state.get("base_url", "").rstrip("/")
    marked = {g: ep for g, ep in state["processed"].items()
              if not ep.get("skipped") and ("file" in ep or "enclosure_url" in ep)}
    pinned = {g: ep for g, ep in state["processed"].items()
              if ep.get("pinned_original_url")}

    def repl_item(m):
        body = m.group(1)
        gm = re.search(r"<guid[^>]*>(.*?)</guid>", body, re.S)
        guid = unescape(gm.group(1)).strip() if gm else ""
        # Strip CDATA wrapper if present (html.unescape doesn't handle it)
        if guid.startswith("<![CDATA[") and guid.endswith("]]>"):
            guid = guid[9:-3].strip()
        ep = marked.get(guid)
        pin = pinned.get(guid)
        if ep and pin:
            # M33 (2026-11-19): a marked episode wins over its pinned
            # original, loudly. A pin only ever stands in for an
            # episode that has not been marked yet (no file, no
            # enclosure_url); the docstring's "clear pinned fields on
            # marking" is now an explicit step in _build_state_entry.
            log(f"  WARNING: feed: {guid} has both a marked enclosure "
                "and a pinned original; serving the marked enclosure")
        if pin and not ep:
            body = re.sub(
                r'<enclosure[^>]*>',
                f'<enclosure url="{pin["pinned_original_url"]}" length="{pin["pinned_original_bytes"]}" type="audio/mpeg"/>',
                body, count=1)
            return "<item>" + body + "</item>"
        if not ep:
            return m.group(0)
        # Direct enclosure URL (e.g. remote-storage link) takes precedence;
        # falls back to base_url + filename for artifact-hosted files.
        if ep.get("enclosure_url"):
            new_url = ep["enclosure_url"]
        elif base and ep.get("file"):
            new_url = base + "/" + ep["file"]
        else:
            return m.group(0)
        # M32 (2026-11-19): the old eager default ep["bytes"] raised
        # KeyError and killed the whole feed when an entry had an
        # enclosure_url but neither enclosure_bytes nor bytes.
        length = ep.get("enclosure_bytes") or ep.get("bytes") or 0
        if not length:
            log(f"  WARNING: feed: {guid} has an enclosure URL but no "
                "byte count; emitting length=0")
        body = re.sub(
            r'<enclosure[^>]*>',
            f'<enclosure url="{new_url}" length="{length}" type="audio/mpeg"/>',
            body, count=1)
        dm = re.search(r"(<description>)(.*?)(</description>)", body, re.S)
        if dm:
            # Handle both CDATA and plain descriptions
            # Marked-ads summary goes at TOP (per the owner 2026-10-09) so it's visible
            inner = dm.group(2)
            html = marked_summary_text(ep)
            if inner.startswith("<![CDATA["):
                new_desc = dm.group(1) + "<![CDATA[" + html + inner[9:-3] + "]]>" + dm.group(3)
            else:
                new_desc = dm.group(1) + html + inner + dm.group(3)
            body = body[:dm.start()] + new_desc + body[dm.end():]
        return "<item>" + body + "</item>"

    feed = re.sub(r"<item>(.*?)</item>", repl_item, original_xml, flags=re.S)
    # Point the feed's self-identifier at our copy, or podcast apps revert
    # the subscription URL to the original feed address.
    feed = re.sub(
        r'<atom:link[^>]*rel="self"[^>]*>',
        f'<atom:link href="{GITHUB_FEED_URL}" type="application/rss+xml" rel="self"/>',
        feed, count=1)
    # Strip the permanent-redirect tag copied from the original feed, or apps
    # follow it straight back to the original address.
    feed = re.sub(r'\s*<itunes:new-feed-url>.*?</itunes:new-feed-url>', '', feed, count=1)
    feed = re.sub(
        r'(<rawvoice:subscribe feed=")[^"]*(")',
        f'\\1{GITHUB_FEED_URL}\\2',
        feed, count=1)
    os.makedirs(site_dir, exist_ok=True)
    with open(os.path.join(site_dir, "feed.xml"), "w") as f:
        f.write(feed)
    return feed


# ---------------------------------------------------------------------------
# Dynamic slots (2026-11-11, DESIGN.md)
# ---------------------------------------------------------------------------
# A second download of the same episode, transcribed and aligned against
# the first, reveals the dynamic-ad slots: the regions where the two
# copies differ. Slots are a boundary source for dynamic ads, not a
# standalone detector: each slot must pass an evidence gate, and a gated
# slot only ever replaces the EDGES of a detector block (or becomes a new
# chapter when the detector found nothing there). Copy A is always the
# published copy; chapters live on the A clock; copy B is never published.
# Its mp3 is PINNED (kept) after alignment so repeat runs align the
# identical A/B pair (slot determinism, 2026-11-18), pruned to the 5
# newest per podcast — except a byte-identical-to-A B, which is still
# deleted (no slots, and a duplicate of A is pure waste).
# ---------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _b_cache_check(b_dir, b_sha256):
    """Invalidate a stale B transcript cache. The B audio changes
    between downloads (ads rotate), so the cached B chunks are only
    valid for the exact B file they were transcribed from. A marker
    file records that file's SHA; a mismatch drops the chunks (and
    transcript.json) so transcribe_chunked re-transcribes. A missing
    marker drops them too: with no marker there is no evidence the
    cache belongs to this B file, so it is treated exactly like a
    mismatch. Without this, a rotated ad would be aligned against
    a stale B transcript and the slots would be wrong."""
    marker = os.path.join(b_dir, "b_sha256.txt")
    if os.path.exists(marker):
        try:
            if open(marker).read().strip() == b_sha256:
                return  # cache matches this B file
        except OSError as e:
            # m17 (2026-11-19): was a silent pass; a failed marker
            # read now logs, and the stale chunks are dropped below.
            log(f"  WARNING: B marker read failed ({e}); dropping "
                "stale B chunks")
        log("  B audio differs from the cached B transcript; "
            "dropping stale B chunks")
    else:
        log("  no B transcript marker; dropping any stale B chunks")
    # m17 (2026-11-19): failed deletes used to be silent; they are
    # now logged. (Whether the marker is still written after a failed
    # delete is m1 — the survivor check below refuses loudly.)
    for f in glob.glob(os.path.join(b_dir, "chunk_*.json")):
        try:
            os.remove(f)
        except OSError as e:
            log(f"  WARNING: could not drop stale B chunk {f} ({e})")
    try:
        os.remove(os.path.join(b_dir, "transcript.json"))
    except OSError as e:
        log(f"  WARNING: could not drop stale B transcript.json ({e})")
    # (m1) Verify the drop succeeded before writing the new marker:
    # a surviving stale chunk would be reused as if it belonged to
    # the new B. Refuse loudly instead of blessing it.
    survivors = glob.glob(os.path.join(b_dir, "chunk_*.json"))
    if os.path.exists(os.path.join(b_dir, "transcript.json")):
        survivors.append(os.path.join(b_dir, "transcript.json"))
    if survivors:
        log(f"  ERROR: stale B chunks could not be removed: {survivors}; "
            "refusing to write the new B marker")
        raise RuntimeError(
            f"_b_cache_check: could not remove stale B cache in {b_dir}: "
            f"{survivors}")
    with open(marker, "w") as f:
        f.write(b_sha256)


def transcribe_b(mp3_path, b_dir, b_sha256=None):
    """Transcribe a B copy through the guarded path (m8): the marker
    check runs first, so a stale B transcript cache can never be
    reused by a direct transcribe_chunked call. All B transcription
    in the repo must go through this helper."""
    if b_sha256 is None:
        b_sha256 = sha256_file(mp3_path)
    _b_cache_check(b_dir, b_sha256)
    return transcribe_chunked(mp3_path, b_dir)


def _cleanup_b_mp3(b_mp3):
    try:
        if os.path.exists(b_mp3):
            os.remove(b_mp3)
    except OSError as e:
        # m17 (2026-11-19): was a silent pass.
        log(f"  WARNING: could not remove B mp3 {b_mp3} ({e})")


def _prune_pinned_b(work, keep=SLOT_PINNED_B_KEEP_SIBLINGS):
    """Prune pinned B mp3s of sibling episodes (same podcast) to the
    `keep` newest, so pinning cannot grow disk without bound. The
    current episode's B is never pruned here; it joins the kept set
    once pinned. A pruned episode re-downloads a fresh B on its next
    run (old behaviour) — only the 5 newest episodes per podcast keep
    run-to-run slot determinism."""
    parent = os.path.dirname(os.path.abspath(work))
    cur = os.path.abspath(os.path.join(work, "B", "original.mp3"))
    paths = [p for p in glob.glob(os.path.join(parent, "*", "B",
                                               "original.mp3"))
             if os.path.abspath(p) != cur]
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for p in paths[keep:]:
        # (m7) Prune the whole B transcript cache + marker + download
        # sidecar together with the mp3; the cache describes audio
        # that no longer exists.
        b_dir = os.path.dirname(p)
        victims = [p]
        victims += glob.glob(os.path.join(b_dir, "chunk_*.json"))
        victims += [os.path.join(b_dir, "transcript.json"),
                    os.path.join(b_dir, "b_sha256.txt"),
                    _download_sidecar_path(p)]
        for v in victims:
            try:
                os.remove(v)
            except OSError as e:
                # m17 (2026-11-19): was a silent pass.
                log(f"  WARNING: could not prune pinned B {v} ({e})")


def _pin_record_path(b_dir):
    return os.path.join(b_dir, "b_pin.json")


def _read_pin_record(b_dir):
    """The N8 pin record, or None when missing/unreadable."""
    try:
        with open(_pin_record_path(b_dir)) as f:
            rec = json.load(f)
        if isinstance(rec, dict) and rec.get("sha256"):
            return rec
    except (OSError, ValueError):
        pass
    return None


def _unverified_pin_expired(b_dir, b_sha):
    """True when an unverified pin has already been reused
    MAX_UNVERIFIED_PIN_USES times (N8). A SHA mismatch means a new
    file, so the count restarts. Expiry deletes the record so the
    fresh download starts a new cycle instead of expiring again."""
    try:
        old = _read_pin_record(b_dir)
        if old is None or old.get("sha256") != b_sha:
            return False
        if int(old.get("uses", 0)) + 1 > MAX_UNVERIFIED_PIN_USES:
            try:
                os.remove(_pin_record_path(b_dir))
            except OSError:
                pass
            return True
    except (OSError, ValueError, TypeError):
        pass
    return False


def _record_pin_acceptance(b_mp3, b_dir, verified, b_dur, b_sha=None):
    """Record the completeness evidence for an accepted pin (N8):
    the file's SHA/bytes/ffprobe duration at pin time, whether the
    download was Content-Length verified, and how many runs have
    reused the pin. Never fails the pin: record errors are logged,
    not raised."""
    try:
        if b_sha is None:
            b_sha = sha256_file(b_mp3)
        old = _read_pin_record(b_dir)
        if old is not None and old.get("sha256") == b_sha:
            uses = int(old.get("uses", 0)) + 1
            first = old.get("accepted_at")
        else:
            uses = 1
            first = None
        _atomic_json_dump(_pin_record_path(b_dir), {
            "sha256": b_sha,
            "bytes": os.path.getsize(b_mp3),
            "duration": b_dur,
            "content_length_verified": bool(verified),
            "accepted_at": first or date.today().isoformat(),
            "uses": uses})
    except (OSError, ValueError, TypeError) as exc:
        log(f"  WARNING: could not record pinned-B acceptance ({exc!r}); "
            "continuing")


def _pinned_b_complete(b_mp3, b_dir, a_duration):
    """Re-validate a pinned B before reuse (m3). Returns True only
    when the file's live ffprobe duration agrees with the download
    sidecar (within 2s) and with the A duration (within the 10% slot
    guard). A missing (legacy) sidecar is accepted once on the
    duration-vs-A check alone, and a sidecar is then written so later
    runs verify identity. Any probe failure or duration move drops
    the pin. SHA alone attests identity, not completeness: a B
    truncated under chunked transfer keeps its SHA forever. (N8) Each
    acceptance records the completeness evidence in b_pin.json; an
    unverified pin expires after MAX_UNVERIFIED_PIN_USES reuses so a
    fresh B is eventually downloaded."""
    sidecar = _download_sidecar_path(b_mp3)
    try:
        b_dur = probe_duration(b_mp3)
    except Exception as exc:
        log(f"  pinned B unprobeable ({exc!r}); dropping pin")
        return False
    if a_duration and abs(b_dur - a_duration) / a_duration > \
            SLOT_GUARD_MAX_DUR_RATIO:
        log(f"  pinned B duration {b_dur:.0f}s vs A {a_duration:.0f}s "
            "outside the slot guard; dropping pin")
        return False
    if os.path.exists(sidecar):
        try:
            with open(sidecar) as f:
                sc = json.load(f)
            b_sha = sha256_file(b_mp3)
            if sc.get("sha256") != b_sha:
                log("  pinned B differs from its download sidecar; "
                    "dropping pin")
                return False
            if sc.get("duration") is not None and \
                    abs(b_dur - sc["duration"]) > 2.0:
                log(f"  pinned B duration moved ({sc['duration']}s "
                    f"recorded, {b_dur:.0f}s now); dropping pin")
                return False
            verified = bool(sc.get("content_length_verified"))
            if not verified:
                log("  pinned B completeness unverified (no "
                    "Content-Length at download); duration guard is "
                    "the only backstop")
                # (N8) An unverified pin is completeness evidence
                # only for a bounded number of reuses: expire it so
                # a fresh B is eventually downloaded.
                if _unverified_pin_expired(b_dir, b_sha):
                    log(f"  unverified pinned B reused "
                        f"{MAX_UNVERIFIED_PIN_USES} times; dropping pin "
                        "so a fresh B is downloaded")
                    return False
            _record_pin_acceptance(b_mp3, b_dir, verified, b_dur, b_sha)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            pass  # unreadable sidecar: fall through to legacy path
    log("  pinned B has no download sidecar (legacy pin); "
        "accepting on duration check alone and recording identity")
    _write_download_sidecar(b_mp3, False)
    _record_pin_acceptance(b_mp3, b_dir, False, b_dur)
    return True


def _slot_overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def _slot_gate_a_side(slot, candidates, a_words, nw_a, sponsors, house_plugs):
    """A-side gate (DESIGN.md section 6a): any detector method segment
    overlaps the slot, or a description-sponsor / house-plug mention
    falls inside it. Returns evidence string ('detector' | 'mention')
    or None."""
    s, e = slot["start"], slot["end"]
    # Detector evidence must come from a method that can mark a block
    # alone (cta/cluster/brand/chapter) and overlap the slot by >=5s
    # AND >=10% of the slot (M17). A 0.1s graze, or overlap with only
    # a low-confidence voter (voice, duplicate, cluster-popular,
    # brand-orphan, url, history-preroll, reverb), is not evidence —
    # the old test passed on any overlap > 0 with any non-reverb
    # method, including voice-only evidence.
    _alone = ("cta", "cluster", "brand", "chapter")
    for cs, ce, _l, m in candidates:
        if m not in _alone:
            continue
        ov = _slot_overlap(s, e, cs, ce)
        if ov >= 5.0 and ov >= 0.10 * (e - s):
            return "detector"
    variants = set()
    for sp in list(sponsors) + list(house_plugs):
        variants |= set(sp.get("variants", []))
    if variants:
        for h in find_mentions(nw_a, variants):
            if s <= a_words[h][0] < e:
                return "mention"
    return None


def _slot_gate_b_side(slot, b_words, nw_b, sponsors, house_plugs, own_domains):
    """B-side gate (DESIGN.md section 6b, stricter): the B-side ad copy
    in the mapped B span (with a margin) contains a sponsor brand
    mention AND a URL/CTA nearby. Returns True/False."""
    bs = slot["b_start"] - SLOT_B_MARGIN_S
    be = slot["b_end"] + SLOT_B_MARGIN_S
    variants = set()
    for sp in list(sponsors) + list(house_plugs):
        variants |= set(sp.get("variants", []))
    if not variants:
        return False
    brand_times = [b_words[h][0] for h in find_mentions(nw_b, variants)
                   if bs <= b_words[h][0] < be]
    if not brand_times:
        return False
    url_idx = {i for _b, i in find_url_mentions(nw_b, own_domains)}
    url_idx |= {i for _b, i in find_bare_urls(nw_b, own_domains)}
    url_times = [b_words[i][0] for i in url_idx
                 if bs <= b_words[i][0] < be]
    # Brand and URL must be near each other (<=60s), not merely in
    # the same (possibly 700s) window (M18).
    return any(abs(bt - ut) <= 60.0
               for bt in brand_times for ut in url_times)


def _gate_slot(slot, a_words, nw_a, b_words, nw_b, candidates,
               sponsors, house_plugs, own_domains):
    """Gate a slot: A-side pass first, then the stricter B-side pass.
    Returns (passed, evidence) with evidence in
    'detector' | 'mention' | 'b_side' | None."""
    ev = _slot_gate_a_side(slot, candidates, a_words, nw_a, sponsors,
                           house_plugs)
    if ev:
        return True, ev
    if _slot_gate_b_side(slot, b_words, nw_b, sponsors, house_plugs,
                         own_domains):
        return True, "b_side"
    return False, None


def _slot_name_from_mentions(slot, a_words, nw_a, sponsors, house_plugs):
    """Name a slot from A-side evidence only (DESIGN.md section 4):
    count slot-internal brand mentions per label across description
    sponsors, house plugs, and the popular-brands list; most mentions
    wins, ties broken description sponsor > house plug > popular brand.
    Returns 'slot:<Label>' or None (unnamed slot — still marked).
    NOTE: this find_mentions call runs AFTER detection and only names
    slot blocks; it feeds no detection method, so it cannot flip a
    sponsor from the orphan path to the brand path (orphan-flip
    constraint, HANDOFF.md 2026-11-10)."""
    s, e = slot["start"], slot["end"]
    # URL/CTA stems inside the slot (m14): a popular brand is named
    # only with an anchor — bare short variants ("mint", "seed",
    # "factor") occur in ordinary speech.
    _url = [(b, i) for b, i in find_bare_urls(nw_a)]
    _url += [(b, i) for b, i in find_url_mentions(nw_a)]
    url_stems = {b.replace(" ", "") for b, i in _url
                 if s <= a_words[i][0] < e}
    pools = [(list(sponsors), 0), (list(house_plugs), 1),
             (list(POPULAR_BRANDS), 2)]
    best = None  # (key, label); key = (count, anchored, -tier, -first)
    for pool, tier in pools:
        for sp in pool:
            variants = set(sp.get("variants", []))
            if not variants:
                continue
            hits = [h for h in find_mentions(nw_a, variants)
                    if s <= a_words[h][0] < e]
            if not hits:
                continue
            stems = {sp["label"].lower().replace(" ", "")}
            stems |= {v.replace(" ", "") for v in variants
                      if " " not in v}
            anchored = bool(stems & url_stems)
            if tier == 2 and not anchored:
                continue
            key = (len(hits), anchored, -tier, -a_words[hits[0]][0])
            if best is None or key > best[0]:
                best = (key, sp["label"])
    return f"slot:{best[1]}" if best else None


def _merge_slots(blocks, gated_slots, a_words, candidates, sponsors,
                 house_plugs):
    """Merge gated slots with detector blocks (DESIGN.md section 7).
    Returns (merged_blocks, sources) with sources parallel to blocks:
    'detector' | 'slot' | 'merged'. Slot edges are used as-is (no
    refinement); slot blocks bypass the 30s/45s floors. Rules
    (fix round 2026-11-19):
    - Density floor (M7): a slot below SLOT_FLOOR_DENSITY_MIN is
      dropped on EVERY path, not just the standalone one.
    - Partial overlap (M8): merging into a block needs >=10s AND
      >=10% of the slot; a grazing touch drops the slot.
    - Multi-overlap (C3): one slot overlapping several detector
      blocks collapses them only when they are the SAME ad (same
      sponsor part); different ads keep their detector blocks.
    - House plugs (m10): the plug veto is per-block — a slot
      overlapping a plug AND a real ad block still merges into the
      ad block; plug blocks are never overwritten.
    - Final pass (M9, M25): merged blocks are clipped against
      neighbours so the result never overlaps, and a partial merge
      that would exceed MAX_AD_LEN keeps the detector block instead.
    A slot partially overlapping only house-plug blocks is dropped;
    the detector block stands."""
    starts = [w[0] for w in a_words]
    ends = [w[1] for w in a_words]
    votes = _word_votes(a_words, candidates)

    def sharpness(t, side):
        v = _edge_sharpness(starts, ends, votes, t, side)
        return round(v, 2) if v is not None else None

    def _clip_against_plugs(ns, ne):
        """Clip a slot-derived span [ns, ne] against house-plug blocks
        overlapping it, on either side (T2-N5). The m10 veto only covers
        plugs the slot overlaps by >=50%; a plug it overlaps partially
        — typically the FOLLOWING plug, which the M9 final clip then
        carves instead of the slot block because the slot block sorts
        first — must survive intact. Returns (ns, ne), or None when the
        merge must be dropped: a plug entirely inside the span (one
        merged block cannot represent a hole), or a clipped remainder
        under 5s (the M9 drop threshold)."""
        for b in blocks:
            if b[1] <= ns or b[0] >= ne:
                continue
            name = ad_chapter_name(b[2], sponsors)
            if not (name and is_house_plug(name, house_plugs)):
                continue
            if b[0] <= ns and b[1] < ne:
                ns = b[1]  # plug straddles the left edge
            elif b[1] >= ne and b[0] > ns:
                ne = b[0]  # plug straddles the right edge
            else:
                return None  # plug entirely inside the span
            if ne - ns < 5:
                return None
        if ne - ns < 5:
            return None
        return (ns, ne)

    merged, sources = [], []
    used = set()
    for slot in sorted(gated_slots, key=lambda s: s["start"]):
        s, e = slot["start"], slot["end"]
        if e - s > MAX_AD_LEN:
            log(f"  slot {s:.1f}-{e:.1f} exceeds MAX_AD_LEN; skipped")
            continue
        # Density floor on every path (M7): a low-density slot must
        # not replace or extend detector edges either — it is a
        # repeated sign-off / outro tail, not an ad read.
        if slot.get("density", 0) < SLOT_FLOOR_DENSITY_MIN:
            log(f"  slot {s:.1f}-{e:.1f} density "
                f"{slot.get('density')} below floor; skipped")
            continue
        overlapping = []
        partial = []
        for i, b in enumerate(blocks):
            ov = _slot_overlap(s, e, b[0], b[1])
            if ov >= 0.5 * (e - s):
                overlapping.append((ov, i, b))
            elif ov > 0:
                partial.append((ov, i, b))
        if not overlapping:
            if partial:
                best = None
                for ov, i, b in partial:
                    nm = ad_chapter_name(b[2], sponsors)
                    if nm and is_house_plug(nm, house_plugs):
                        continue
                    if best is None or ov > best[0]:
                        best = (ov, i, b)
                if best is not None:
                    _ov, i, b = best
                    # Minimum partial overlap (M8): a grazing touch
                    # must not absorb the whole slot.
                    if _ov < 10.0 or _ov < 0.10 * (e - s):
                        log(f"  slot {s:.1f}-{e:.1f} partially overlaps "
                            f"detector block {b[0]:.1f}-{b[1]:.1f} by "
                            f"only {_ov:.1f}s; dropped")
                        continue
                    pct = 100.0 * _ov / (e - s) if e > s else 0.0
                    ns, ne = min(s, b[0]), max(e, b[1])
                    # A merge that would exceed MAX_AD_LEN keeps the
                    # detector block instead (M25).
                    if ne - ns > MAX_AD_LEN:
                        log(f"  slot {s:.1f}-{e:.1f} merged block "
                            f"{ns:.1f}-{ne:.1f} exceeds MAX_AD_LEN; "
                            "keeping detector block")
                        merged.append(b)
                        sources.append("detector")
                        used.add(i)
                        continue
                    # T2-N5: the merged span must not swallow a
                    # partially-overlapped plug block.
                    _clipped = _clip_against_plugs(ns, ne)
                    if _clipped is None:
                        log(f"  slot {s:.1f}-{e:.1f} merged span "
                            f"{ns:.1f}-{ne:.1f} would overwrite a "
                            "house-plug block; dropped")
                        continue
                    ns, ne = _clipped
                    log(f"  slot {s:.1f}-{e:.1f} partially overlaps "
                        f"detector block {b[0]:.1f}-{b[1]:.1f} "
                        f"({_ov:.1f}s, {pct:.0f}% of slot); merged into "
                        f"block -> {ns:.1f}-{ne:.1f}")
                    merged.append((ns, ne, b[2], sharpness(ns, "start"),
                                   sharpness(ne, "end")))
                    sources.append("merged")
                    used.add(i)
                    continue
                log(f"  slot {s:.1f}-{e:.1f} partially overlaps only "
                    "house-plug blocks; dropped")
                continue
            merged.append((s, e, slot["label"],
                           sharpness(s, "start"), sharpness(e, "end")))
            sources.append("slot")
            continue
        # House-plug blocks are never overwritten by a slot (m10):
        # the veto is per-block — a slot overlapping a plug AND a
        # real ad block still merges into the ad block.
        _nonplug = []
        for _ov, _i, b in overlapping:
            name = ad_chapter_name(b[2], sponsors)
            if name and is_house_plug(name, house_plugs):
                continue
            _nonplug.append((_ov, _i, b))
        if not _nonplug:
            log(f"  slot {s:.1f}-{e:.1f} overlaps only house-plug "
                "blocks; detector block stands")
            continue
        overlapping = _nonplug
        if len(overlapping) == 1:
            _ov, i, b = overlapping[0]
            label = b[2]
            # T2-N5: clip the slot span against partially-overlapped
            # plug blocks on either side before emitting it.
            _clipped = _clip_against_plugs(s, e)
            if _clipped is None:
                log(f"  slot {s:.1f}-{e:.1f} would overwrite a "
                    "house-plug block; dropped")
                continue
            ns, ne = _clipped
            used.add(i)
            merged.append((ns, ne, label, sharpness(ns, "start"),
                           sharpness(ne, "end")))
            sources.append("merged")
            continue
        # One slot, several detector blocks (C3): collapse only when
        # they are the SAME ad (same sponsor part). Different ads keep
        # their detector blocks — collapsing would delete a whole ad.
        # T2-N3: a single common part that is generic ('ad',
        # 'dry-island', ...) is not an identity — two different ads
        # both stripped to 'ad' must not collapse.
        _c3_parts = {_xep_part(o[2][2]) for o in overlapping}
        if len(_c3_parts) != 1 or \
                _is_generic_part(next(iter(_c3_parts))):
            log(f"  slot {s:.1f}-{e:.1f} overlaps "
                f"{len(overlapping)} different-ad detector blocks; "
                "keeping detector blocks")
            continue
        overlapping.sort(key=lambda t: -t[0])
        top = overlapping[0][0]
        tied = [o for o in overlapping if abs(o[0] - top) < 1e-9]
        labels = {o[2][2] for o in tied}
        if len(labels) == 1:
            label = tied[0][2][2]
        else:
            label = slot["label"]
        # T2-N5: clip the collapsed span against partially-overlapped
        # plug blocks, as in the single-overlap path above.
        _clipped = _clip_against_plugs(s, e)
        if _clipped is None:
            log(f"  slot {s:.1f}-{e:.1f} would overwrite a "
                "house-plug block; dropped")
            continue
        ns, ne = _clipped
        for _ov, i, _b in overlapping:
            used.add(i)
        merged.append((ns, ne, label, sharpness(ns, "start"),
                       sharpness(ne, "end")))
        sources.append("merged")
    for i, b in enumerate(blocks):
        if i not in used:
            merged.append(b)
            sources.append("detector")
    # Final pass (M9, M25): sort by start, clip any block that starts
    # before the previous block's end, drop blocks clipped below 5s,
    # and drop anything still over MAX_AD_LEN (backstop — the
    # per-path checks above should already prevent it).
    out, out_sources = [], []
    for b, src in sorted(zip(merged, sources), key=lambda p: p[0][0]):
        s, e = b[0], b[1]
        if out and s < out[-1][1]:
            s = out[-1][1]
            if e - s < 5:
                log(f"  drop block {b[0]:.1f}-{b[1]:.1f} ('{b[2]}'): "
                    f"clipped to {e - s:.1f}s by neighbour")
                continue
            b = (s, e, b[2], sharpness(s, "start"), b[4])
        if e - s > MAX_AD_LEN:
            log(f"  drop block {s:.1f}-{e:.1f} ('{b[2]}'): "
                "exceeds MAX_AD_LEN")
            continue
        out.append(b)
        out_sources.append(src)
    return out, out_sources


def _write_align_result(work, a_sha256, b_sha256, aligned, gated, rejected,
                        guards):
    """Persist the alignment record (DESIGN.md section 9): the density
    slots (NOT the v1 merge spans the prototype saved), both SHAs, the
    gate outcome per slot, and any tripped guards. Alignment itself is
    re-run on every B flow (~1s); this file is the audit record, keyed
    by both SHAs, not a skip-cache."""
    payload = {
        "aligner_version": aligned.get("aligner_version",
                                       SLOT_ALIGNER_VERSION),
        "a_sha256": a_sha256, "b_sha256": b_sha256,
        "differing_token_rate": aligned.get("differing_token_rate"),
        "n_anchors": aligned.get("n_anchors"),
        "n_anchor_candidates": aligned.get("n_anchor_candidates"),
        "a_duration": aligned.get("a_duration"),
        "b_duration": aligned.get("b_duration"),
        "slots": aligned.get("slots", []),
        "gated": gated,
        "rejected": rejected,
        "guards": guards,
    }
    with open(os.path.join(work, "align_result.json"), "w") as f:
        json.dump(payload, f, indent=2)


def run_slot_flow(work, a_words, a_duration, a_mp3_path, a_sha256, b_url,
                  sponsors, desc_sponsors, house_plugs, own_domains,
                  candidates, blocks, b_mp3_path=None):
    """Run the B flow for one episode (DESIGN.md sections 2-8).
    work: episode work dir (B transcript cached in work/B/,
    alignment record in work/align_result.json).
    Returns a dict: status in 'slots' | 'no_slots' | 'static' |
    'b_failed' | 'slots_rejected'; on 'slots', blocks/sources are the
    merged detector+slot blocks; otherwise blocks/sources are the
    detector-only blocks. A B-download failure degrades to detector-only;
    a B-transcription crash propagates (crash protocol: fix root cause,
    re-run the episode from the top)."""
    try:
        from slot_align import align_words
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from slot_align import align_words
    b_dir = os.path.join(work, "B")
    os.makedirs(b_dir, exist_ok=True)
    b_mp3 = b_mp3_path or os.path.join(b_dir, "original.mp3")
    result = {"status": "b_failed", "blocks": blocks,
              "sources": ["detector"] * len(blocks),
              "slots": [], "rejected": [], "b_sha256": None,
              "b_duration": None, "differing_token_rate": None,
              "n_anchors": None}
    # B download: reuse a pinned B when one is valid (slot
    # determinism, 2026-11-18 — a fresh download every run rotates the
    # dynamic ads and re-transcription moves slot edges, so repeat runs
    # of the same episode gave different slots). A pin is valid when
    # the B mp3 is present and b_sha256.txt records the same SHA;
    # _b_cache_check below then keeps the matching transcript cache,
    # and transcribe_chunked reuses the cached chunks, so a re-run
    # aligns the identical A/B pair. Otherwise download fresh
    # (existing behaviour, including the probe/supplied-path case).
    _prune_pinned_b(work)
    pinned = False
    if b_mp3_path is None and os.path.exists(b_mp3):
        marker = os.path.join(b_dir, "b_sha256.txt")
        if os.path.exists(marker):
            try:
                if open(marker).read().strip() == sha256_file(b_mp3):
                    # (m3) SHA attests identity, not completeness:
                    # re-validate the pinned file's live duration
                    # against the download sidecar (and the A) before
                    # trusting the pin.
                    pinned = _pinned_b_complete(b_mp3, b_dir, a_duration)
                    if pinned:
                        log("  reusing pinned B download "
                            f"({os.path.getsize(b_mp3)} bytes)")
            except OSError:
                pass
        if not pinned:
            try:
                os.remove(b_mp3)
            except OSError:
                pass
            # (N2) A failed delete must not fall through to download():
            # download() trusts any present file matching its sidecar,
            # so the rejected B would be reused with 0 network calls.
            # Degrade to detector-only instead.
            if os.path.exists(b_mp3):
                log(f"  ERROR: rejected pinned B {b_mp3} could not be "
                    "removed; refusing to download over it "
                    "(detector-only output)")
                result["rejected"] = [{"reason": "pin_drop_failed",
                                       "detail": b_mp3}]
                return result
    try:
        if b_mp3_path is None and not pinned:
            download(b_url, b_mp3, retries=3)
    except Exception as exc:
        log(f"  B download failed ({exc!r}); detector-only output")
        return result
    try:
        b_sha256 = sha256_file(b_mp3)
    except OSError as exc:
        log(f"  B file unreadable ({exc!r}); detector-only output")
        return result
    result["b_sha256"] = b_sha256
    if b_sha256 == a_sha256:
        log("  B download is byte-identical to A; no slots")
        _cleanup_b_mp3(b_mp3)
        result["status"] = "no_slots"
        return result
    # The B transcript cache is only valid for this exact B file;
    # transcribe_b runs the marker check first (m8).
    # B transcription (a crash here propagates — crash protocol).
    b_words, b_duration = transcribe_b(b_mp3, b_dir, b_sha256)
    result["b_duration"] = b_duration
    # Alignment.
    try:
        aligned = align_words(a_words, b_words)
    except Exception as exc:
        log(f"  slot alignment failed ({exc!r}); detector-only output")
        result["status"] = "slots_rejected"
        result["rejected"] = [{"reason": "align_error",
                               "detail": repr(exc)}]
        return result
    result["differing_token_rate"] = aligned["differing_token_rate"]
    result["n_anchors"] = aligned["n_anchors"]
    guards = []
    if aligned["differing_token_rate"] > SLOT_GUARD_MAX_DIFF_RATE:
        guards.append("diff_rate")
    if a_duration and abs(b_duration - a_duration) / a_duration > \
            SLOT_GUARD_MAX_DUR_RATIO:
        guards.append("duration_ratio")
    # Drop implausibly long slots before gating.
    kept, rejected = [], []
    for s in aligned["slots"]:
        if s["end"] - s["start"] > MAX_AD_LEN:
            rejected.append({"start": s["start"], "end": s["end"],
                             "reason": "too_long"})
        else:
            kept.append(s)
    # Gate.
    nw_a = norm_words(a_words)
    nw_b = norm_words(b_words)
    gated = []
    for s in kept:
        passed, ev = _gate_slot(s, a_words, nw_a, b_words, nw_b,
                                candidates, desc_sponsors, house_plugs,
                                own_domains)
        if passed:
            s = dict(s)
            s["evidence"] = ev
            gated.append(s)
        else:
            rejected.append({"start": s["start"], "end": s["end"],
                             "reason": "no_ad_evidence"})
    result["rejected"] = rejected
    # Slot-share guard.
    total_slot_s = sum(s["end"] - s["start"] for s in gated)
    if a_duration and total_slot_s > SLOT_GUARD_MAX_SLOT_SHARE * a_duration:
        guards.append("slot_share")
    if guards:
        log(f"  slot sanity guard tripped ({', '.join(guards)}); "
            "slots rejected")
        result["status"] = "slots_rejected"
        result["guards"] = guards
        result["slots"] = gated  # kept for review, not merged
        _write_align_result(work, a_sha256, b_sha256, aligned, gated,
                            rejected, guards)
        return result
    if not gated:
        log("  no gated slots; detector-only output")
        result["status"] = "no_slots"
        _write_align_result(work, a_sha256, b_sha256, aligned, gated,
                            rejected, guards)
        return result
    # Name gated slots (A-side evidence only), then merge.
    for s in gated:
        s["label"] = _slot_name_from_mentions(s, a_words, nw_a,
                                              desc_sponsors, house_plugs)
    merged, sources = _merge_slots(blocks, gated, a_words, candidates,
                                   sponsors, house_plugs)
    result["status"] = "slots"
    result["blocks"] = merged
    result["sources"] = sources
    result["slots"] = gated
    _write_align_result(work, a_sha256, b_sha256, aligned, gated,
                        rejected, guards)
    return result


def _dynamic_flag_status(pcfg, state):
    """Decide whether this feed needs a probe (DESIGN.md section 1).
    Returns (mode, value): ('config', bool) when podcasts.yaml sets
    dynamic_ads; ('flag', bool) when state holds a fresh (<=30d) probe
    flag; (None, None) when a probe is needed."""
    if pcfg and "dynamic_ads" in pcfg:
        return "config", bool(pcfg["dynamic_ads"])
    rec = state.get("dynamic_ads") or {}
    checked = rec.get("checked")
    if rec.get("status") is not None and checked:
        try:
            age = (date.today() - date.fromisoformat(checked)).days
        except ValueError:
            age = None
        if age is not None and age <= SLOT_PROBE_RECHECK_DAYS:
            return "flag", bool(rec["status"])
    return None, None


def _save_state_quietly(state):
    """Persist state now; log (never raise) on failure (M34).

    Used for the dynamic flag / attempts counters, which must reach
    disk as soon as they change -- not only at the end of a
    successful process_episode()."""
    try:
        save_state(state)
    except Exception as exc:
        log(f"  WARNING: immediate state save failed ({exc!r}); continuing")


def _record_dynamic_flag(state, status, probe_episode, source):
    state["dynamic_ads"] = {
        "status": bool(status), "checked": date.today().isoformat(),
        "probe_episode": probe_episode, "source": source,
        "consecutive_no_slots": 0,
    }


def _run_dynamic_for_episode(item, pcfg, state, work, mp3_path, words,
                             duration, sponsors, desc_sponsors,
                             own_domains, candidates, blocks):
    """Probe/flag/config logic + B flow for one episode (DESIGN.md
    sections 1-2, 9). Returns a dict with the blocks to publish
    (merged or detector-only), their sources, and the per-episode
    dynamic fields for the state entry and report."""
    prev = state.get("processed", {}).get(item["guid"], {})
    attempts = prev.get("dynamic_attempts", 0)
    mode, value = _dynamic_flag_status(pcfg, state)
    info = {"dynamic_status": "static", "dynamic_slots": [],
            "dynamic_slots_rejected": [], "dynamic_attempts": attempts,
            "dynamic_pending": False, "b_sha256": None,
            "b_duration": None, "differing_token_rate": None,
            "n_anchors": None, "blocks": blocks,
            "sources": ["detector"] * len(blocks)}
    # Transcript rule (2026-11-11, owner): a feed carrying
    # podcast:transcript tags already has a second transcript to compare
    # against (the future transcript-differential stage), so the
    # dynamic-slot second download is skipped entirely — no probe
    # download, no B transcription, no B flow. Podcast-level rule: an
    # episode without its own tag still gets no B flow. An explicit
    # dynamic_ads: true in podcasts.yaml beats this (config wins over
    # auto-detection). The feed may still serve dynamic ads; the B flow
    # is skipped because the transcript comparison supersedes it.
    if item.get("feed_has_transcript") and not (mode == "config" and value):
        log("  feed carries podcast:transcript tags; skipping second "
            "download (publisher transcript is the comparison)")
        _record_dynamic_flag(state, False, item["title"], "transcript")
        _save_state_quietly(state)  # M34: persist the flag now
        info["dynamic_status"] = "transcript"
        return info
    if mode in ("config", "flag") and not value:
        return info  # static feed; nothing to do
    a_sha256 = sha256_file(mp3_path)
    b_mp3 = os.path.join(work, "B", "original.mp3")
    if mode is None:
        # Probe: one second download, byte-compare (DESIGN.md section 1).
        log("  probing feed for dynamic ads (second download)")
        try:
            if os.path.exists(b_mp3):
                os.remove(b_mp3)
            os.makedirs(os.path.dirname(b_mp3), exist_ok=True)
            download(item["audio_url"], b_mp3, retries=3)
        except Exception as exc:
            log(f"  probe download failed ({exc!r}); detector-only output")
            flow = {"status": "b_failed", "blocks": blocks,
                    "sources": ["detector"] * len(blocks),
                    "slots": [], "rejected": [], "b_sha256": None,
                    "b_duration": None, "differing_token_rate": None,
                    "n_anchors": None}
        else:
            b_sha256 = sha256_file(b_mp3)
            if b_sha256 == a_sha256:
                log("  probe: second download is byte-identical; "
                    "feed is static")
                _cleanup_b_mp3(b_mp3)
                _record_dynamic_flag(state, False, item["title"], "probe")
                _save_state_quietly(state)  # M34: persist the flag now
                info["b_sha256"] = b_sha256
                return info
            log("  probe: second download differs; feed is dynamic")
            _record_dynamic_flag(state, True, item["title"], "probe")
            _save_state_quietly(state)  # M34: persist the flag now
            flow = run_slot_flow(work, words, duration, mp3_path, a_sha256,
                                 item["audio_url"], sponsors, desc_sponsors,
                                 HOUSE_PLUGS, own_domains, candidates, blocks,
                                 b_mp3_path=b_mp3)
    else:
        # Dynamic by config or fresh flag: run the B flow (it downloads).
        flow = run_slot_flow(work, words, duration, mp3_path, a_sha256,
                             item["audio_url"], sponsors, desc_sponsors,
                             HOUSE_PLUGS, own_domains, candidates, blocks)
    # Fold the flow result into the per-episode record.
    info["dynamic_status"] = flow["status"]
    info["dynamic_slots"] = flow.get("slots", [])
    info["dynamic_slots_rejected"] = flow.get("rejected", [])
    info["b_sha256"] = flow.get("b_sha256")
    info["b_duration"] = flow.get("b_duration")
    info["differing_token_rate"] = flow.get("differing_token_rate")
    info["n_anchors"] = flow.get("n_anchors")
    info["blocks"] = flow.get("blocks", blocks)
    info["sources"] = flow.get("sources", ["detector"] * len(blocks))
    # Attempts / pending (DESIGN.md section 9).
    if flow["status"] == "b_failed":
        attempts += 1
        info["dynamic_attempts"] = attempts
        info["dynamic_pending"] = attempts < SLOT_MAX_ATTEMPTS
        if not info["dynamic_pending"]:
            log("  B download failed 3 times; giving up on this episode")
    else:
        info["dynamic_attempts"] = 0
        info["dynamic_pending"] = False
    # M34: persist the attempts / pending counters NOW, not only at
    # the end of process_episode() -- a crash after the B flow used
    # to lose them, so the episode was retried forever with no cap.
    # Stored on the episode entry (minimal entry if this is a first
    # run); process_episode() overwrites them with the same values.
    _attempt_entry = state.setdefault("processed", {}).setdefault(
        item["guid"], {})
    _attempt_entry.setdefault("title", item["title"])
    _attempt_entry["dynamic_attempts"] = info["dynamic_attempts"]
    _attempt_entry["dynamic_pending"] = info["dynamic_pending"]
    _save_state_quietly(state)
    # No-slots streak: three consecutive dynamic-flagged episodes with
    # no slots clears the flag, so the next episode re-probes. (Config
    # feeds have no flag to clear.)
    rec = state.get("dynamic_ads")
    if rec and mode == "flag":
        if flow["status"] == "no_slots":
            streak = rec.get("consecutive_no_slots", 0) + 1
            rec["consecutive_no_slots"] = streak
            if streak >= 3:
                log(f"  no slots in {streak} consecutive dynamic "
                    "episodes; clearing the dynamic flag "
                    "(next episode re-probes)")
                state.pop("dynamic_ads", None)
        elif flow["status"] == "slots":
            rec["consecutive_no_slots"] = 0
        # T3-N8: persist the streak / flag-clear NOW, as the attempts
        # already are -- a crash after the B flow used to lose it.
        _save_state_quietly(state)
    return info


def slots_only_mode(slug, items, channel_link):
    """Standalone slot inspection (DESIGN.md section 10): print the
    gated slots for one already-processed episode as a manual-marking
    aid. Writes no pipeline state and publishes nothing (the A/B
    transcript and align_result.json caches may be written; state.json
    is not)."""
    item = None
    for it in items:
        s = re.sub(r"[^a-z0-9]+", "-", it["title"].lower()).strip("-")[:60]
        if s == slug:
            item = it
            break
    if not item:
        raise SystemExit(f"no episode with slug {slug!r} in the feed")
    work = os.path.join(EP_DIR, slug)
    mp3_path = os.path.join(work, "original.mp3")
    if not os.path.exists(mp3_path):
        raise SystemExit(f"no A download at {mp3_path}; "
                         "process the episode first")
    # (N5) Load the A transcript through the guarded live path
    # (transcribe_chunked): identity-checked, corrupt chunks treated
    # as misses and re-transcribed — never align from a stale cache.
    # A stale cache is re-transcribed here instead of being used.
    words, duration = transcribe_chunked(mp3_path, work)
    if not words:
        raise SystemExit("no A transcript cache; process the episode first")
    try:
        _check_transcript_plausible(words, duration, item["title"])
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    # (M3) Drop stale out-of-range words a glob reader would otherwise
    # include (chunks from an older, longer cut).
    words = [w for w in words if w[0] < duration]
    if not words:
        raise SystemExit("no A transcript words within the audio duration; "
                         "process the episode first")
    sponsors = extract_sponsors(item["description"])
    desc_sponsors = list(sponsors)
    sponsors += [dict(p) for p in HOUSE_PLUGS]
    own_domains = extract_own_domains(item["description"], channel_link,
                                      desc_sponsors)
    silences = find_silences(mp3_path, work, min_dur=0.8)
    existing_ms = read_chapters(mp3_path)
    id3_chapters = [(ms / 1000.0, t) for ms, t in existing_ms]
    chapters = _merge_chapters(id3_chapters, item.get("rss_chapters", []))
    candidates_out = []
    _channel_m = re.search(r"https?://([^/\"' >]+)", channel_link) if channel_link else None
    _channel_host = _channel_m.group(1) if _channel_m else None
    blocks, _suspects = detect_ad_blocks(words, sponsors, silences, mp3_path,
                                         chapters, own_domains=own_domains,
                                         candidates_out=candidates_out,
                                         own_identity=({_domain_stem(_channel_host)} | own_domains)
                                         if _channel_host else own_domains,
                                         audio_duration=duration)
    a_sha256 = sha256_file(mp3_path)
    flow = run_slot_flow(work, words, duration, mp3_path, a_sha256,
                         item["audio_url"], sponsors, desc_sponsors,
                         HOUSE_PLUGS, own_domains, candidates_out, blocks)
    print(f"episode: {item['title']}")
    print(f"dynamic status: {flow['status']}")
    for s in flow.get("slots", []):
        print(f"  slot {s['start']:.1f}-{s['end']:.1f} "
              f"({s['end'] - s['start']:.1f}s, evidence {s.get('evidence')}, "
              f"label {s.get('label')})")
    for r in flow.get("rejected", []):
        print(f"  rejected {r['start']:.1f}-{r['end']:.1f}: {r['reason']}")
    if flow["status"] == "slots":
        print("merged blocks:")
        for (s, e, l, _ss, _es), src in zip(flow["blocks"],
                                            flow["sources"]):
            print(f"  {s:.1f}-{e:.1f} {l} [{src}]")


def _record_history(item, blocks, block_sources, duration):
    """Append this episode's blocks to the per-podcast history file
    (the learning component's collection step, 2026-11-14; see
    history/README.md). Called once, after a successful detection.
    Never fails an episode run: any problem is logged and the run
    continues. A zero-block episode still records one zero-ad row so
    the average ad count stays honest (see podcast_history)."""
    try:
        import hashlib as _hashlib
        try:
            import podcast_history as _ph
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import podcast_history as _ph
        version = _hashlib.md5(open(__file__, "rb").read()).hexdigest()[:12]
        result = _ph.append_episode(
            _ph.default_history_dir(), PODCAST_ID, item["guid"],
            item["title"], duration, item.get("pubDate", ""),
            blocks, block_sources, HOUSE_PLUGS, version)
        if result["ok"] and result["rows_written"]:
            _verb = ("replaced" if result.get("replaced") else "appended")
            log(f"  history: {_verb} {result['rows_written']} rows "
                f"to {PODCAST_ID}.csv")
        elif result["ok"]:
            log(f"  history: skipped ({result['skipped']})")
        else:
            log(f"  history: write failed ({result['error']}); continuing")
    except Exception as exc:  # never fail an episode run on history
        log(f"  history: skipped ({exc!r}); continuing")


def _check_transcript_plausible(words, duration, title):
    """(M2) Never publish a zero/near-zero transcript as "no ads": an
    empty or implausible transcript means transcription failed, not
    that the episode has no ads. Frozen SB minimum is 126 words/min;
    20 words/min is ~6x below any real episode. (N3) Absolute floors,
    independent of the rate test: a transcript under 60s or under 20
    words is implausible whatever its rate — a 5-word/5s transcript
    at a normal rate used to pass."""
    if not words or duration < 60 or len(words) < 20 or (
            duration >= 60 and len(words) / duration * 60 < 20):
        raise RuntimeError(
            f"refusing to publish {title}: implausible transcript "
            f"({len(words)} words over {duration:.0f}s); transcription "
            f"failed, not detection")


# Sentinel for _build_state_entry(): keep the old value of a field
# the caller did not recompute (used by the B-only retry, M6).
_KEEP_OLD = object()


def _build_state_entry(old, item, out_name, out_bytes, marked_dur,
                       state_blocks, dyn, td_result=_KEEP_OLD,
                       td_corr=_KEEP_OLD, missed=_KEEP_OLD,
                       suspects_summaries=_KEEP_OLD,
                       suspects_reel_name=_KEEP_OLD,
                       suspects_reel_bytes=_KEEP_OLD):
    """Build the state entry for a (re-)marked episode (M6/M33, 2026-11-19).

    Starts from a copy of the previous entry instead of a bare
    literal dict, so publish fields (enclosure_*, suspects_*), legacy
    fields (ads_reel*, ads_removed, removed_seconds, reverted) and
    any other writer's fields survive a re-run -- the old wholesale
    rewrite dropped them all (state-repair landmine, known issue 5;
    M6's feed-enclosure consequence). Explicit steps on every
    marking: pinned_original_* fields are cleared (the write_feed
    docstring's promise, now implemented rather than a side effect);
    skipped/reason are cleared (a processed episode is not skipped).
    When the episode was already published and its block edges
    changed, republish_needed is set so publish_github re-publishes
    it (feed_pushed tracks whether that push was verified)."""
    entry = dict(old) if old else {}
    for k in [k for k in entry if k.startswith("pinned_original_")]:
        entry.pop(k)
        log(f"  cleared pinned_original field {k} on marking "
            f"{item['title']}")
    entry.pop("skipped", None)
    entry.pop("reason", None)
    old_blocks = [(b.get("start"), b.get("end"), b.get("sponsor"),
                   b.get("source")) for b in
                  (old or {}).get("blocks", [])] if old else []
    new_blocks = [(b["start"], b["end"], b.get("sponsor"),
                   b.get("source")) for b in state_blocks]
    entry.update({
        "title": item["title"], "pubDate": item["pubDate"],
        "file": out_name,
        "bytes": out_bytes,
        "ads_marked": len(state_blocks),
        "ads_marked_seconds": sum(b["end"] - b["start"]
                                  for b in state_blocks),
        "duration_seconds": marked_dur,
        "blocks": state_blocks,
        "dynamic_status": dyn["dynamic_status"],
        "dynamic_slots": dyn["dynamic_slots"],
        "dynamic_slots_rejected": dyn["dynamic_slots_rejected"],
        "dynamic_attempts": dyn["dynamic_attempts"],
        "dynamic_pending": dyn["dynamic_pending"],
    })
    if td_result is not _KEEP_OLD:
        entry.update({
            "transcript_diff_status": td_result["status"],
            "transcript_diff_coverage": td_result["coverage"],
            "transcript_diff_gaps": len(td_result["gaps"]),
        })
    if td_corr is not _KEEP_OLD:
        entry.update({
            "transcript_diff_corroborated": len(td_corr["corroborated"]),
            "transcript_diff_held": len(td_corr["held"]),
        })
    if missed is not _KEEP_OLD:
        entry["missed_sponsors"] = [m["label"] for m in missed]
    if suspects_summaries is not _KEEP_OLD:
        entry["suspects"] = suspects_summaries
    if suspects_reel_name is not _KEEP_OLD:
        entry["suspects_reel"] = suspects_reel_name
    if suspects_reel_bytes is not _KEEP_OLD:
        entry["suspects_reel_bytes"] = suspects_reel_bytes
    if old and old.get("enclosure_url"):
        if old_blocks != new_blocks:
            entry["republish_needed"] = True
            log(f"  blocks changed since publish; flagging "
                f"{item['title']} for re-publish")
        else:
            entry.pop("republish_needed", None)
    return entry


def _write_marked_file(mp3_path, out_path, blocks, sponsors, base_chapters,
                       duration):
    """ffmpeg -c copy + ad-chapter marking (shared by process_episode
    and the B-only retry, M6). Returns (ad_spans, marked_chapters).

    Atomic (resumable runs, 2026-11-21): the copy and the chapter
    embedding happen on a temp file in the same directory; the final
    path is replaced only once the file is complete and fsynced, so a
    kill can never leave a partial file at the final path (the 289
    re-process was killed mid-mux and left a 75 MB partial file
    there). A leftover <out-stem>.tmp.<pid>.mp3 file is inert (never
    swept automatically — a concurrent writer's temp must not be
    deleted)."""
    ad_spans = _ad_chapter_spans(blocks, sponsors, HOUSE_PLUGS)
    marked_chapters = insert_ad_chapters(base_chapters, ad_spans, duration)
    tmp_out = _tmp_path(out_path)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mp3_path,
                    "-c", "copy", tmp_out], check=True)
    if marked_chapters:
        write_chapters(tmp_out, marked_chapters, duration)
        log(f"marked {len(blocks)} ad chapters in "
            f"{os.path.basename(out_path)}")
    else:
        log("no confident ad blocks; publishing whole with original chapters")
    _fsync_file(tmp_out)
    os.replace(tmp_out, out_path)
    _fsync_dir(os.path.dirname(out_path) or ".")
    return ad_spans, marked_chapters


def _retry_dynamic_only(item, state, channel_link, pcfg):
    """B-only retry for a dynamic_pending episode (M6, 2026-11-19).

    Reuses the on-disk A transcript (work/transcript.json) and the
    stored blocks from state; runs only the B flow + rescue + the
    marked-file rebuild. Never re-downloads A, never re-transcribes,
    never re-detects -- so a rotated A cannot change the blocks.
    Returns (True, "") on success, (False, reason) when the B-only
    path is unavailable (the caller falls back to full
    process_episode). Raises on a failure inside the B flow (no
    fallback: falling back would re-detect on possibly rotated A,
    the defect itself). Candidates are [] (they exist only as
    detection output); _merge_slots uses them for sharpness only,
    so sharpness falls back to slot defaults here."""
    guid = item["guid"]
    old = state.get("processed", {}).get(guid) or {}
    slug = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")[:60]
    work = os.path.join(EP_DIR, slug)
    mp3_path = os.path.join(work, "original.mp3")
    tpath = os.path.join(work, "transcript.json")
    if not old.get("blocks"):
        return False, "no stored blocks in state"
    if not os.path.exists(mp3_path):
        return False, "A mp3 missing from work dir"
    if not os.path.exists(tpath):
        return False, "A transcript.json missing from work dir"
    try:
        with open(tpath) as f:
            tdata = json.load(f)
        words = [(w[0], w[1], w[2]) for w in tdata["words"]]
        duration = tdata["duration"]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        return False, f"A transcript unreadable ({e})"
    # (N10) Verify the stored transcript belongs to the on-disk A
    # before trusting it: a rotated/restored A with a stale
    # transcript.json would rebuild chapters for the wrong audio.
    # Any failure here returns unavailable; the caller falls back
    # to full process_episode (which re-downloads + re-transcribes).
    try:
        a_sha = sha256_file(mp3_path)
        a_size = os.path.getsize(mp3_path)
        ident = json.load(open(os.path.join(work, "a_identity.json")))
        if ident.get("sha256") != a_sha or ident.get("size") != a_size:
            return False, "A transcript identity mismatch with on-disk A"
        if ident.get("duration") is not None and \
                abs(float(ident["duration"]) - float(duration)) > 1.0:
            return False, "A transcript duration mismatch with identity"
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as e:
        return False, f"A transcript identity unreadable ({e})"
    try:
        _check_transcript_plausible(words, duration, item["title"])
    except RuntimeError as e:
        return False, f"A transcript implausible ({e})"
    sponsors = extract_sponsors(item["description"])
    desc_sponsors = list(sponsors)
    sponsors += [dict(p) for p in HOUSE_PLUGS]
    own_domains = extract_own_domains(item["description"], channel_link,
                                      desc_sponsors)
    blocks = [(b["start"], b["end"], b.get("sponsor"),
               b.get("start_sharpness"), b.get("end_sharpness"))
              for b in old["blocks"]]
    block_sources = [b.get("source", "detector") for b in old["blocks"]]
    dyn = _run_dynamic_for_episode(
        item, pcfg, state, work, mp3_path, words, duration, sponsors,
        desc_sponsors, own_domains, [], blocks)
    blocks = dyn["blocks"]
    block_sources = dyn["sources"]
    from split_rescue import rescue_fused_blocks
    blocks, block_sources, _rescues = rescue_fused_blocks(
        blocks, [], words, sponsors, sources=block_sources)
    existing_ms = read_chapters(mp3_path)
    id3_chapters = [(ms / 1000.0, t) for ms, t in existing_ms]
    chapters = _merge_chapters(id3_chapters, item.get("rss_chapters", []))
    if chapters:
        base = chapters
    else:
        log("no pre-existing chapters; auto-generating from transcript")
        base = [(ms / 1000.0, t) for ms, t in generate_chapters(words, duration)]
    out_name = f"{slug}-marked.mp3"
    out_path = os.path.join(SITE_DIR, out_name)
    ad_spans, _mc = _write_marked_file(mp3_path, out_path, blocks,
                                       sponsors, base, duration)
    marked_dur = probe_duration(out_path)

    def chapter_for_block(s):
        for s0, e0, t in ad_spans:
            if s0 <= s < e0:
                return t
        return None

    state_blocks = []
    for (s, e, l, ss, es), src in zip(blocks, block_sources):
        name = ad_chapter_name(l, sponsors)
        title = chapter_for_block(s) or (
            f"Muse Detected Ad For {name}" if name else "Muse Detected Ad")
        state_blocks.append({"start": s, "end": e, "sponsor": l,
                             "seconds": e - s, "chapter": title,
                             "start_sharpness": ss, "end_sharpness": es,
                             "source": src})
    # T3-N9: rebuild the entry from the freshly read disk entry, so
    # a concurrent writer's fields for this episode (e.g. a publish
    # record) survive the re-mark; fall back to the in-memory entry
    # when the disk state cannot be read. Held under the state lock
    # together with the save so no writer can slip between the read
    # and the write. (Coord integration: state_lock() itself raises
    # TypeError when the podcast was never initialised, so the whole
    # locked block is guarded -- a lock failure falls back to the
    # pre-N9 build-from-memory + save, which is what the N10 driver
    # exercises.)
    try:
        with state_lock():
            try:
                _fresh_old = _load_state_raw().get(
                    "processed", {}).get(guid)
            except Exception as exc:
                log(f"  WARNING: fresh state re-read failed ({exc!r}); "
                    "rebuilding the entry from the in-memory state")
                _fresh_old = old
            state["processed"][guid] = _build_state_entry(
                _fresh_old, item, out_name, os.path.getsize(out_path),
                marked_dur, state_blocks, dyn)
            save_state(state)
    except TypeError:
        log("  WARNING: state lock unavailable (podcast not "
            "initialised); rebuilding the entry from the in-memory "
            "state")
        state["processed"][guid] = _build_state_entry(
            old, item, out_name, os.path.getsize(out_path),
            marked_dur, state_blocks, dyn)
        save_state(state)
    _record_history(item, blocks, block_sources, duration)
    log(f"done (B-only retry): {item['title']} -- marked {len(blocks)} "
        "ads as chapters")
    return True, ""


# ---------------------------------------------------------------------------
# Run checkpoints (resumable runs, 2026-11-21): a killed process_episode
# re-enters at the first incomplete stage instead of starting over.
# The checkpoint is a record, never proof on its own: every claimed
# stage is verified against its on-disk artifact before it is trusted
# (see the _verify_stage_* helpers). Written with _atomic_json_dump,
# so a kill can never leave a truncated checkpoint. Generic: the
# checkpoint carries the podcast / guid / slug as data, never as
# branching.
# ---------------------------------------------------------------------------
CHECKPOINT_VERSION = 1
_CHECKPOINT_STAGES = ["download_a", "transcribe_a", "detect", "dynamic",
                      "rescue_diff", "write_marked", "write_suspects",
                      "state_saved"]


def _checkpoint_path(work):
    return os.path.join(work, "checkpoint.json")


def _block_fingerprint(blocks):
    """Stable identity for a block list: sha256 of the canonical JSON
    (start/end/sharpness rounded to 3 decimals, label as-is)."""
    canon = [[round(float(s), 3), round(float(e), 3), str(l),
              round(float(ss), 3), round(float(es), 3)]
             for (s, e, l, ss, es) in blocks]
    return hashlib.sha256(
        json.dumps(canon, sort_keys=True).encode()).hexdigest()


def _suspects_fingerprint(suspects):
    """Stable identity for a suspects list (start/end rounded to 3
    decimals, label as-is)."""
    canon = [[round(float(s), 3), round(float(e), 3), str(l)]
             for (s, e, l) in suspects]
    return hashlib.sha256(
        json.dumps(canon, sort_keys=True).encode()).hexdigest()


def _pipeline_md5():
    try:
        return hashlib.md5(open(os.path.abspath(__file__), "rb").read()
                           ).hexdigest()
    except OSError:
        return None


def _checkpoint_new(podcast, guid, slug):
    return {"version": CHECKPOINT_VERSION, "podcast": podcast,
            "guid": guid, "slug": slug,
            "pipeline_md5": _pipeline_md5(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stages": {}}


def _read_checkpoint(path):
    try:
        with open(path) as f:
            ck = json.load(f)
        if not isinstance(ck, dict) or ck.get("version") != CHECKPOINT_VERSION:
            return None
        if not isinstance(ck.get("stages"), dict):
            return None
        return ck
    except (OSError, ValueError, AttributeError):
        return None


def _checkpoint_record(work, ck, stage, artifact, path=None):
    """Record one completed stage, atomically (temp + replace via
    _atomic_json_dump). Recording a stage overwrites only that stage;
    later claims are left in place and re-verified at their own
    boundary before they are ever trusted."""
    p = path or _checkpoint_path(work)
    ck.setdefault("stages", {})[stage] = {
        "completed_at": datetime.now(timezone.utc).isoformat(), **artifact}
    _atomic_json_dump(p, ck)
    return ck


def _verify_stage_download_a(mp3_path):
    """True when the A file matches its download sidecar."""
    return _verify_download_sidecar(mp3_path)


def _verify_stage_transcribe_a(work, mp3_path):
    """True when the A transcript cache belongs to this exact A file
    and is complete: identity matches the file, every in-range chunk
    exists with a matching config, and the chunk word sum equals
    transcript.json."""
    try:
        ident = json.load(open(os.path.join(work, "a_identity.json")))
        if ident.get("sha256") != sha256_file(mp3_path):
            return False
        if ident.get("size") != os.path.getsize(mp3_path):
            return False
        dur = float(ident.get("duration") or 0)
        if dur <= 0:
            return False
        if not _transcript_cache_complete(work, dur):
            return False
        tj = json.load(open(os.path.join(work, "transcript.json")))
        n_words = len(tj.get("words", []))
        if not n_words:
            return False
        total = 0
        for f in glob.glob(os.path.join(work, "chunk_*.json")):
            total += len(json.load(open(f)).get("words", []))
        return total == n_words
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _verify_stage_write_marked(out_path, claimed, fingerprint, duration):
    """True when the final marked file matches the checkpoint claim
    (block fingerprint + size + SHA-256 + probe duration) for the
    current blocks."""
    try:
        if claimed.get("block_fingerprint") != fingerprint:
            return False
        if os.path.getsize(out_path) != claimed.get("bytes"):
            return False
        if sha256_file(out_path) != claimed.get("sha256"):
            return False
        if abs(probe_duration(out_path) - duration) > 1.0:
            return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def _verify_stage_write_suspects(out_path, claimed):
    """True when the final suspects reel matches the checkpoint claim
    (size + SHA-256). The suspects-list fingerprint is checked by the
    caller alongside this."""
    try:
        return (os.path.getsize(out_path) == claimed.get("bytes") and
                sha256_file(out_path) == claimed.get("sha256"))
    except (OSError, ValueError, TypeError):
        return False


def process_episode(item, state, channel_link=None, pcfg=None,
                    resume_ck=None, checkpoint_path=None):
    slug = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")[:60]
    work = os.path.join(EP_DIR, slug)
    os.makedirs(work, exist_ok=True)
    mp3_path = os.path.join(work, "original.mp3")
    # Run checkpoint (resumable runs, 2026-11-21): a record of
    # completed stages in the work dir, so a killed run re-enters at
    # the first incomplete stage. A checkpoint from another episode
    # is ignored (full run).
    _ck_path = checkpoint_path or _checkpoint_path(work)
    if resume_ck is not None and (
            resume_ck.get("guid") != item.get("guid") or
            resume_ck.get("slug") != slug):
        log("  WARNING: resume checkpoint is for a different episode; "
            "ignoring it (full run)")
        resume_ck = None
    ck = resume_ck if resume_ck is not None else _checkpoint_new(
        PODCAST_ID, item.get("guid"), slug)
    if resume_ck is not None:
        # Resume assessment: which already-completed stages verify
        # against their on-disk artifacts right now (later stages are
        # re-verified at their own boundaries, once their inputs
        # exist). This is the record the 289 kill lacked — how far
        # the run got before it died.
        _verified = []
        if _verify_stage_download_a(mp3_path):
            _verified.append("download_a")
        if _verify_stage_transcribe_a(work, mp3_path):
            _verified.append("transcribe_a")
        log(f"  resuming from checkpoint: already verified: "
            f"{', '.join(_verified) if _verified else 'none'}")
    download(item["audio_url"], mp3_path)
    try:
        _sc = json.load(open(_download_sidecar_path(mp3_path)))
        _dl = {"bytes": _sc.get("bytes"), "sha256": _sc.get("sha256"),
               "duration": _sc.get("duration")}
    except (OSError, ValueError):
        _dl = {"bytes": os.path.getsize(mp3_path),
               "sha256": sha256_file(mp3_path), "duration": None}
    _checkpoint_record(work, ck, "download_a", _dl, path=_ck_path)

    log(f"transcribing {item['title']}")
    words, duration = transcribe_chunked(mp3_path, work)
    log(f"transcript: {len(words)} words, {duration:.0f}s audio")
    try:
        _ident = json.load(open(os.path.join(work, "a_identity.json")))
    except (OSError, ValueError):
        _ident = {}
    _checkpoint_record(work, ck, "transcribe_a", {
        "sha256": _ident.get("sha256"), "size": _ident.get("size"),
        "duration": duration, "words": len(words),
        "chunks": len(glob.glob(os.path.join(work, "chunk_*.json")))},
        path=_ck_path)
    _check_transcript_plausible(words, duration, item["title"])

    sponsors = extract_sponsors(item["description"])
    desc_sponsors = list(sponsors)
    sponsors += [dict(p) for p in HOUSE_PLUGS]
    log(f"sponsors from RSS: {[s['label'] for s in sponsors]}")
    # Show-own domains (imp5): spoken URLs on the show's own domain are
    # content, not ad evidence. Derived generically from the RSS channel
    # link + non-sponsored description links, minus sponsor stems.
    own_domains = extract_own_domains(item["description"], channel_link,
                                      desc_sponsors)
    if own_domains:
        log(f"own domains excluded from URL evidence: {sorted(own_domains)}")
    # Always run detection: CTA/duplicate/voice/cluster-popular methods are
    # sponsor-independent (2026-10-12: skipping them for sponsor-less
    # episodes meant Legends 90 was published without any ad check).
    silences = find_silences(mp3_path, work, min_dur=0.8)
    # Chapter-marker signal (6th detection method, 2026-10-14): publisher-
    # labeled ad chapters from ID3 tags and RSS psc:chapters.
    existing_ms = read_chapters(mp3_path)
    id3_chapters = [(ms / 1000.0, t) for ms, t in existing_ms]
    chapters = _merge_chapters(id3_chapters, item.get("rss_chapters", []))
    if chapters:
        log(f"{len(chapters)} chapters available for chapter-marker detection")
    candidates_out = []  # raw method segments, for the dynamic-slot gate
    _channel_m = re.search(r"https?://([^/\"' >]+)", channel_link) if channel_link else None
    _channel_host = _channel_m.group(1) if _channel_m else None
    blocks, suspects = detect_ad_blocks(words, sponsors, silences, mp3_path,
                                        chapters, own_domains=own_domains,
                                        candidates_out=candidates_out,
                                        own_identity=({_domain_stem(_channel_host)} | own_domains)
                                        if _channel_host else own_domains,
                                        audio_duration=duration)
    # Cross-episode recurrence, positive direction (2026-11-11, KEPT):
    # recurring sponsor-copy spans from prior episodes of this show
    # extend already-detected blocks to the span's bounds. Runs after
    # detection and before the dynamic-slot flow (the harness order);
    # the slot flow can still replace a block's edges with the more
    # accurate slot edges. Any failure degrades to detector-only.
    try:
        blocks = _apply_xep_corroboration(blocks, words, sponsors,
                                           silences, candidates_out,
                                           EP_DIR, slug)
    except Exception as exc:
        log(f"  xep-corroboration skipped ({exc!r}); detector-only output")
    # Missed-sponsor check covers description sponsors only: a house plug
    # not mentioned in a given episode is normal, not a miss.
    missed = check_missed_sponsors(words, desc_sponsors, blocks)
    if not sponsors:
        log("no sponsors listed in description; sponsor-independent detection only")
    # Checkpoint: detection complete. On a resume, a changed
    # fingerprint means the re-run produced different blocks than the
    # killed run — warn and continue with the fresh result (the fresh
    # result wins; the fingerprint is a tripwire, not a gate).
    _fp = _block_fingerprint(blocks)
    _old_fp = ck.get("stages", {}).get("detect", {}).get("block_fingerprint")
    if _old_fp and _old_fp != _fp:
        log("  WARNING: resumed detection produced different blocks than "
            "the killed run (fingerprint changed); continuing with the "
            "fresh result")
    _checkpoint_record(work, ck, "detect", {"blocks": len(blocks),
                                            "block_fingerprint": _fp},
                       path=_ck_path)

    # Dynamic slots (2026-11-11, DESIGN.md): second-download alignment
    # as a boundary source for dynamic ads. Runs after A detection,
    # before chapter building; any B failure degrades to detector-only.
    dyn = _run_dynamic_for_episode(
        item, pcfg, state, work, mp3_path, words, duration, sponsors,
        desc_sponsors, own_domains, candidates_out, blocks)
    blocks = dyn["blocks"]
    block_sources = dyn["sources"]
    _checkpoint_record(work, ck, "dynamic", {
        "dynamic_status": dyn["dynamic_status"],
        "b_sha256": dyn["b_sha256"], "b_duration": dyn["b_duration"],
        "n_slots": len(dyn["dynamic_slots"]),
        "blocks": len(blocks),
        "block_fingerprint": _block_fingerprint(blocks)}, path=_ck_path)

    # Back-to-back split (2026-11-13): rescue a second sponsor's ad
    # trimmed from the head/tail of a fused heat-map region (see
    # sb_eval/ab/back_to_back_split/NOTE.md). Post-processing on the
    # final blocks + raw candidates, after the slot flow so rescued
    # blocks keep source "rescue" in block_sources. KEPT 2026-11-14
    # (owner's decision): the A/B rule fails on the SB letter (median
    # 4.7->4.8s, within-1s 26.4%->25.7%) because SB merges
    # back-to-back pairs into single labels; every rescue block
    # verified a real ad.
    from split_rescue import rescue_fused_blocks
    blocks, block_sources, _rescues = rescue_fused_blocks(
        blocks, candidates_out, words, sponsors, sources=block_sources)

    # Publisher-transcript differential (idea 6): corroborate marked
    # blocks, hold the rest as suspects. Never marks on its own;
    # any failure degrades to detector-only (status logged).
    td_enabled = bool(pcfg.get("transcript_diff", True)) if pcfg else True
    td_result = {"status": "skipped", "transcript_url": None,
                 "transcript_type": None, "coverage": None,
                 "n_anchors": 0, "gaps": [], "error": None}
    td_corr = {"corroborated": [], "held": []}
    if td_enabled and item.get("transcript_tags"):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import transcript_diff as _td
        td_result = _td.run_transcript_diff(
            words, item.get("transcript_tags") or [], work_dir=work,
            sponsors=[b["label"] for b in
                      extract_sponsors(item.get("description") or "")])
    if td_result["status"] == "ok" and td_result["gaps"]:
        td_corr = _td.corroborate_blocks(td_result["gaps"], blocks)
        for c in td_corr["corroborated"]:
            i = c["block_index"]
            # Record the boost; never lower, cap at 0.95 (the boost
            # is recorded, not applied to a numeric confidence —
            # AdChisel blocks carry no confidence field today; see
            # §5 for the field this lands in).
            block_sources[i] = block_sources[i] + "+transcript-diff"
            log(f"  transcript-diff corroborates block "
                f"{blocks[i][0]:.0f}-{blocks[i][1]:.0f}s "
                f"(+{c['boost']:.2f}"
                f"{' offset-confirmed' if c['offset_confirmed'] else ''})")
        for g in td_corr["held"]:
            suspects.append((g["start"], g["end"], "transcript-diff:held"))
            log(f"  SUSPECT transcript-diff held gap "
                f"{g['start']:.1f}-{g['end']:.1f}s "
                f"({g['duration']:.0f}s, {g['words']} words) — held, not marked")
    elif td_result["status"] in ("error", "unreliable"):
        log(f"  transcript-diff: {td_result['status']} "
            f"({td_result.get('error') or 'coverage ' + str(td_result.get('coverage'))}); "
            f"detector-only output")
    elif td_result["status"] == "skipped":
        # m17 (2026-11-19): was explicitly silent ("silent, as
        # today"); log the reason so a feed losing its transcript
        # tags (or a config change disabling the diff) is visible.
        _td_reason = ("disabled in config" if not td_enabled
                      else "no podcast:transcript tags on this episode")
        log(f"  transcript-diff: skipped ({_td_reason}); "
            "detector-only output")
    _checkpoint_record(work, ck, "rescue_diff", {
        "blocks": len(blocks),
        "block_fingerprint": _block_fingerprint(blocks)}, path=_ck_path)

    # Chapters model (2026-10-28, per the owner): no cutting. The published
    # file is the full original episode with detected ads marked as
    # chapters ("Muse Detected Ad For <name>") embedded in the audio.
    # Base chapters: publisher chapters when present, else auto-generated
    # from transcript topic changes (same as before, minus cut remapping).
    if chapters:
        base = chapters
    else:
        log("no pre-existing chapters; auto-generating from transcript")
        base = [(ms / 1000.0, t) for ms, t in generate_chapters(words, duration)]
    out_name = f"{slug}-marked.mp3"
    out_path = os.path.join(SITE_DIR, out_name)
    # Resume: if the marked file is already complete and verified for
    # these exact blocks, skip the ffmpeg mux entirely; the chapter
    # spans are recomputed in memory (pure functions) for the state /
    # report steps below.
    _wm = ck.get("stages", {}).get("write_marked") or {}
    if _wm and _verify_stage_write_marked(
            out_path, _wm, _block_fingerprint(blocks), duration):
        log("  resuming: marked file already complete and verified; "
            "skipping the ffmpeg write")
        ad_spans = _ad_chapter_spans(blocks, sponsors, HOUSE_PLUGS)
        marked_chapters = insert_ad_chapters(base, ad_spans, duration)
    else:
        ad_spans, marked_chapters = _write_marked_file(
            mp3_path, out_path, blocks, sponsors, base, duration)
        _checkpoint_record(work, ck, "write_marked", {
            "bytes": os.path.getsize(out_path),
            "sha256": sha256_file(out_path),
            "duration": probe_duration(out_path),
            "block_fingerprint": _block_fingerprint(blocks)}, path=_ck_path)
    marked_dur = probe_duration(out_path)
    # Build suspect reel (2026-10-22, per the owner): segments the pipeline
    # was unsure about, pasted chronologically. The owner reviews these
    # instead of the whole episode. NOT a feed item -- uploaded as a
    # release asset and linked in the nightly message.
    suspects_reel_name = None
    if suspects:
        suspects_reel_name = f"{slug}-suspects.mp3"
        suspects_reel_path = os.path.join(SITE_DIR, suspects_reel_name)
        _ws = ck.get("stages", {}).get("write_suspects") or {}
        _sfp = _suspects_fingerprint(suspects)
        if _ws and _ws.get("suspects_fingerprint") == _sfp and \
                _verify_stage_write_suspects(suspects_reel_path, _ws):
            log("  resuming: suspects reel already complete and verified; "
                "skipping the reel build")
        else:
            build_segment_reel(mp3_path, suspects, suspects_reel_path, work)
            _checkpoint_record(work, ck, "write_suspects", {
                "bytes": os.path.getsize(suspects_reel_path),
                "sha256": sha256_file(suspects_reel_path),
                "suspects_fingerprint": _sfp}, path=_ck_path)
        log(f"suspects-reel: {suspects_reel_name} ({len(suspects)} segments)")

    def chapter_for_block(s):
        for s0, e0, t in ad_spans:
            if s0 <= s < e0:
                return t
        return None

    state_blocks = []
    for (s, e, l, ss, es), src in zip(blocks, block_sources):
        name = ad_chapter_name(l, sponsors)
        title = chapter_for_block(s) or (
            f"Muse Detected Ad For {name}" if name else "Muse Detected Ad")
        state_blocks.append({"start": s, "end": e, "sponsor": l,
                             "seconds": e - s, "chapter": title,
                             "start_sharpness": ss, "end_sharpness": es,
                             "source": src})
    report = {
        "title": item["title"], "pubDate": item["pubDate"],
        "original_seconds": duration, "duration_seconds": marked_dur,
        "blocks": [{"start": s, "end": e, "sponsor": l,
                    "start_sharpness": ss, "end_sharpness": es,
                    "source": src}
                   for (s, e, l, ss, es), src in zip(blocks, block_sources)],
        "ad_chapters": [{"start": s, "end": e, "title": t} for s, e, t in ad_spans],
        "missed_sponsors": missed,
        "suspects": [{"start": s, "end": e, "label": l} for s, e, l in suspects],
        "dynamic_status": dyn["dynamic_status"],
        "dynamic_slots": dyn["dynamic_slots"],
        "dynamic_slots_rejected": dyn["dynamic_slots_rejected"],
        "dynamic_b_sha256": dyn["b_sha256"],
        "dynamic_b_duration": dyn["b_duration"],
        "differing_token_rate": dyn["differing_token_rate"],
        "dynamic_n_anchors": dyn["n_anchors"],
        "transcript_diff": {
            "status": td_result["status"],
            "transcript_url": td_result["transcript_url"],
            "transcript_type": td_result["transcript_type"],
            "coverage": td_result["coverage"],
            "n_anchors": td_result["n_anchors"],
            "gaps": td_result["gaps"],
            "corroborated": [{"block_index": c["block_index"],
                              "boost": c["boost"],
                              "offset_confirmed": c["offset_confirmed"]}
                             for c in td_corr["corroborated"]],
            "held": td_corr["held"],
            "error": td_result["error"],
        },
    }
    _atomic_json_dump(os.path.join(work, "report.json"), report, indent=2)

    # T3-N9: rebuild the entry from the freshly read disk entry, so
    # a concurrent writer's fields for this episode (e.g. a publish
    # record) survive the re-mark; fall back to the in-memory entry
    # when the disk state cannot be read. Held under the state lock
    # together with the save so no writer can slip between the read
    # and the write. (Coord integration: same guard as the
    # _retry_dynamic_only block -- a state_lock() failure falls back
    # to the pre-N9 build-from-memory + save.)
    try:
        with state_lock():
            try:
                _fresh_old = _load_state_raw().get(
                    "processed", {}).get(item["guid"])
            except Exception as exc:
                log(f"  WARNING: fresh state re-read failed ({exc!r}); "
                    "rebuilding the entry from the in-memory state")
                _fresh_old = state["processed"].get(item["guid"])
            state["processed"][item["guid"]] = _build_state_entry(
                _fresh_old, item, out_name,
                os.path.getsize(out_path), marked_dur, state_blocks, dyn,
                td_result, td_corr, missed,
                [{"label": l, "seconds": e - s} for s, e, l in suspects],
                suspects_reel_name,
                os.path.getsize(os.path.join(SITE_DIR, suspects_reel_name))
                if suspects_reel_name else 0)
            save_state(state)
    except TypeError:
        log("  WARNING: state lock unavailable (podcast not "
            "initialised); rebuilding the entry from the in-memory "
            "state")
        state["processed"][item["guid"]] = _build_state_entry(
            state["processed"].get(item["guid"]), item, out_name,
            os.path.getsize(out_path), marked_dur, state_blocks, dyn,
            td_result, td_corr, missed,
            [{"label": l, "seconds": e - s} for s, e, l in suspects],
            suspects_reel_name,
            os.path.getsize(os.path.join(SITE_DIR, suspects_reel_name))
            if suspects_reel_name else 0)
        save_state(state)
    _record_history(item, blocks, block_sources, duration)
    _checkpoint_record(work, ck, "state_saved", {
        "ads_marked": len(state_blocks),
        "block_fingerprint": _block_fingerprint(blocks)}, path=_ck_path)
    log(f"done: {item['title']} -- marked {len(blocks)} ads as chapters")
    return report


# Whole-episode crashes before main() stops retrying an episode (M34).
PROCESS_MAX_CRASHES = 3


def _record_process_crash(state, guid, title):
    """Count a whole-episode crash toward the retry cap (M34).

    Crash counts live in a separate top-level dict (not in the
    processed entry) so a crashed-but-unprocessed episode stays
    retryable for crashes 1-2 and is skipped, with a log line, at 3."""
    crashes = state.setdefault("process_crashes", {})
    crashes[guid] = crashes.get(guid, 0) + 1
    log(f"  process crash #{crashes[guid]} for {title} "
        f"(cap {PROCESS_MAX_CRASHES})")
    _save_state_quietly(state)


def _resume_episode(slug, checkpoint_path, items, state, channel_link, pcfg):
    """One-off resume of a killed run (resumable runs, 2026-11-21).

    Resolves the episode from the feed by derived slug, loads the
    checkpoint (default <work>/checkpoint.json), and calls
    process_episode with it. A checkpoint from a different pipeline
    version, or for a different episode, is ignored (full run).
    Exits non-zero when the slug is not in the feed — resume needs
    the feed item for audio_url and description. Publishes nothing
    itself; main() regenerates the feed afterwards (M26)."""
    item = None
    for it in items:
        _s = re.sub(r"[^a-z0-9]+", "-", it["title"].lower()).strip("-")[:60]
        if _s == slug:
            item = it
            break
    if item is None:
        raise SystemExit(f"--resume: no feed item with slug {slug!r}")
    work = os.path.join(EP_DIR, slug)
    ck_path = checkpoint_path or _checkpoint_path(work)
    ck = _read_checkpoint(ck_path)
    if ck is None:
        log(f"  --resume {slug}: no readable checkpoint at {ck_path}; "
            "full run from the top")
    elif ck.get("guid") != item.get("guid") or ck.get("slug") != slug:
        log("  --resume: checkpoint is for a different episode; "
            "ignoring it (full run)")
        ck = None
    elif ck.get("pipeline_md5") != _pipeline_md5():
        log("  --resume: checkpoint was written by a different pipeline "
            "version; ignoring it (full run)")
        ck = None
    else:
        log(f"  --resume {slug}: checkpoint at {ck_path} "
            f"({len(ck.get('stages', {}))} stages recorded)")
    process_episode(item, state, channel_link, pcfg, resume_ck=ck,
                    checkpoint_path=ck_path)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--podcast", required=True,
                    help="podcast id from podcasts.yaml")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N newest unprocessed episodes (0 = no limit)")
    ap.add_argument("--slots-only", default=None, metavar="SLUG",
                    help="print the gated dynamic slots for one already-"
                    "processed episode (slug as in the work dir) and exit; "
                    "writes no pipeline state and publishes nothing")
    ap.add_argument("--resume", default=None, metavar="SLUG",
                    help="resume a killed run for one episode (slug as in "
                    "the work dir); re-enters at the first incomplete "
                    "checkpoint stage")
    ap.add_argument("--checkpoint", default=None, metavar="PATH",
                    help="checkpoint file for --resume (default "
                    "<work>/checkpoint.json)")
    args = ap.parse_args()

    cfg, pcfg = init_podcast(args.podcast)
    log(f"podcast: {pcfg['name']} ({args.podcast})")
    state = load_state()
    xml = fetch_feed()
    items = parse_items(xml)
    # Transcript rule (2026-11-11, owner): a feed carrying
    # podcast:transcript tags already has a second transcript to compare
    # against, so the dynamic-slot second download is skipped for the
    # whole podcast. Feed-level fact, attached to each item so the
    # B-download retry path sees it too.
    feed_has_transcript = any(it.get("has_transcript") for it in items)
    for it in items:
        it["feed_has_transcript"] = feed_has_transcript
    channel_link = parse_channel_link(xml)
    log(f"feed has {len(items)} items")
    if args.resume:
        _resume_episode(args.resume, args.checkpoint, items, state,
                        channel_link, pcfg)
        # M26: the feed is regenerated on EVERY run, so a resumed run
        # that changed the state cannot leave a stale feed behind.
        try:
            write_feed(state, SITE_DIR, xml)
            log("feed regenerated")
        except Exception as exc:
            log(f"ERROR: feed regeneration failed: {exc!r}")
            raise
        return
    if args.slots_only:
        slots_only_mode(args.slots_only, items, channel_link)
        return
    # Dynamic-slot B-download retry (DESIGN.md section 9): episodes
    # whose B flow failed get retried before new episodes are processed.
    # M6 (2026-11-19): the retry is B-only (_retry_dynamic_only) when
    # the A transcript + stored blocks are on disk; only when that
    # path is unavailable does it fall back to full process_episode.
    # M34: episodes that crashed >= PROCESS_MAX_CRASHES times are
    # skipped, not retried forever.
    pending = [(g, ep) for g, ep in state["processed"].items()
               if ep.get("dynamic_pending")
               and ep.get("dynamic_attempts", 0) < SLOT_MAX_ATTEMPTS
               and state.get("process_crashes", {}).get(g, 0)
               < PROCESS_MAX_CRASHES]
    if pending:
        by_guid = {it["guid"]: it for it in items}
        for guid, ep in pending:
            item = by_guid.get(guid)
            if not item or not item.get("audio_url"):
                # m17a (2026-11-19): was a bare continue -- the
                # pending flag then sat in state forever with no
                # future run mentioning it.
                log(f"  WARNING: pending B retry {guid} "
                    f"({ep.get('title', '?')}) not in feed or has no "
                    "audio_url; leaving dynamic_pending set")
                continue
            log(f"retrying B flow for pending episode: {item['title']}")
            try:
                done, why = _retry_dynamic_only(item, state, channel_link,
                                                pcfg)
                if not done:
                    log(f"  B-only retry unavailable ({why}); "
                        "full re-process")
                    process_episode(item, state, channel_link, pcfg)
            except Exception as exc:
                _record_process_crash(state, guid, item["title"])
                log(f"FAILED retry on {item['title']}: {exc!r}")
    new_items = [it for it in reversed(items)
                 if it["guid"] and it["guid"] not in state["processed"]
                 and it["audio_url"]
                 and state.get("process_crashes", {}).get(it["guid"], 0)
                 < PROCESS_MAX_CRASHES]
    capped = [it for it in reversed(items)
              if it["guid"] and it["guid"] not in state["processed"]
              and it["audio_url"]
              and state.get("process_crashes", {}).get(it["guid"], 0)
              >= PROCESS_MAX_CRASHES]
    for it in capped:
        log(f"  skipping {it['title']}: crashed {PROCESS_MAX_CRASHES} "
            "times; needs manual attention (see state process_crashes)")
    if args.limit:
        new_items = new_items[-args.limit:]
    if not new_items:
        log("no new episodes")
    for item in new_items:
        log(f"processing: {item['title']}")
        try:
            process_episode(item, state, channel_link, pcfg)
        except Exception as exc:
            _record_process_crash(state, item["guid"], item["title"])
            log(f"FAILED on {item['title']}: {exc!r}")
            # leave unprocessed so a later run retries (up to the cap)
    # M26 (2026-11-19): the feed is regenerated on EVERY run, not
    # only when a new episode was processed -- a failed feed write
    # used to persist forever because the next (empty) run returned
    # early before write_feed().
    try:
        write_feed(state, SITE_DIR, xml)
        log("feed regenerated")
    except Exception as exc:
        log(f"ERROR: feed regeneration failed: {exc!r}")
        raise


if __name__ == "__main__":
    main()
