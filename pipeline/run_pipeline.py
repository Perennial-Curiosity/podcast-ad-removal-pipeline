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
     in the full original audio -- nothing is cut
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

import difflib
import json
import os
import re
from datetime import datetime, timezone
import subprocess
import sys
import time
import urllib.request
from html import unescape

# The model snapshot lives in the default HF cache; force offline so the
# (broken-proxy) network is never touched.
os.environ["HF_HUB_OFFLINE"] = "1"

BASE_DIR = os.path.expanduser("~/workspace/podcast-ads")
MODEL_DIR = os.path.join(BASE_DIR, "model")
CONFIG_PATH = os.path.join(BASE_DIR, "podcasts.yaml")

# Ad-block bounds (seconds)
MIN_AD_LEN = 45
# Heat-map blocks need only 2+ agreeing votes, so they may be shorter
# than single-method blocks: dynamic spots run 30-31s (Unsubscribe 286,
# 2026-10-26). Single-method blocks keep the 45s floor.
HEATMAP_MIN_AD_LEN = 30
MAX_AD_LEN = 12 * 60
# Chunk size for transcription (seconds) — keeps each job short
TRANSCRIBE_CHUNK = 180

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


def init_podcast(podcast_id):
    """Load config and set per-podcast globals. Returns (cfg, pcfg)."""
    global FEED_URL, EP_DIR, SITE_DIR, STATE_PATH, HOUSE_PLUGS, GITHUB_FEED_URL
    cfg, pcfg = load_config(podcast_id)
    paths = podcast_paths(podcast_id)
    FEED_URL = pcfg["feed_url"]
    EP_DIR = paths["ep_dir"]
    SITE_DIR = paths["site_dir"]
    STATE_PATH = paths["state_path"]
    HOUSE_PLUGS = house_plugs(pcfg)
    GITHUB_FEED_URL = github_feed_url(cfg, pcfg)
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
                return r.read().decode("utf-8", errors="replace")
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
            return out.stdout.decode("utf-8", errors="replace")
        log(f"  fetch_feed curl fallback failed (rc={out.returncode})")
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
        root = re.sub(r"\.(com|net|org|io|ai|tv|co|us|tools|app|dev|xyz|online|store|tech|site|cloud|me|cc|gg|life|world|news|media|agency|digital|solutions|systems|network|security)$", "", text)
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
            root = re.sub(r"\.(com|net|org|io|ai|tv|co|us|tools|app|dev|xyz|online|store|tech|site|cloud|me|cc|gg|life|world|news|media|agency|digital|solutions|systems|network|security)$", "", host)
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


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"processed": {}, "base_url": ""}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def download(url, dest, retries=6):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log(f"already downloaded: {dest}")
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
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            log(f"saved {os.path.getsize(dest)} bytes")
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
            if attempt < retries:
                wait = 20 * attempt
                log(f"retrying download in {wait}s...")
                time.sleep(wait)
    raise last_err


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def load_model():
    from faster_whisper import WhisperModel
    return WhisperModel("base", device="cpu", compute_type="int8",
                        local_files_only=True)


def transcribe_file(model, wav_path, **kwargs):
    """Transcribe via a numpy array.

    faster-whisper 1.2.1 passes metadata_errors to av.open(), which the
    installed PyAV 19 no longer accepts — feeding raw audio avoids av.
    """
    import numpy as np
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", wav_path, "-ar", "16000", "-ac", "1",
         "-f", "f32le", "-acodec", "pcm_f32le", "-"],
        capture_output=True, check=True).stdout
    audio = np.frombuffer(out, dtype=np.float32)
    return model.transcribe(audio, **kwargs)


def transcribe_chunked(mp3_path, work_dir):
    """Transcribe in short chunks with word timestamps; resumable."""
    from faster_whisper import WhisperModel
    os.makedirs(work_dir, exist_ok=True)
    duration = probe_duration(mp3_path)
    model = load_model()
    words = []  # (start, end, text)
    n_chunks = int(duration // TRANSCRIBE_CHUNK) + 1
    for i in range(n_chunks):
        start = i * TRANSCRIBE_CHUNK
        if start >= duration:
            break
        chunk_json = os.path.join(work_dir, f"chunk_{i:03d}.json")
        if os.path.exists(chunk_json):
            with open(chunk_json) as f:
                data = json.load(f)
            words.extend([(w[0], w[1], w[2]) for w in data["words"]])
            continue
        chunk_wav = os.path.join(work_dir, f"chunk_{i:03d}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(start),
             "-t", str(TRANSCRIBE_CHUNK), "-i", mp3_path,
             "-ar", "16000", "-ac", "1", chunk_wav],
            check=True)
        segments, _ = transcribe_file(model, chunk_wav, word_timestamps=True)
        cwords = []
        for seg in segments:
            for w in (seg.words or []):
                cwords.append((w.start + start, w.end + start, w.word))
        with open(chunk_json, "w") as f:
            json.dump({"words": cwords}, f)
        os.remove(chunk_wav)
        words.extend(cwords)
        log(f"transcribed chunk {i + 1}/{n_chunks}")
    full = os.path.join(work_dir, "transcript.json")
    with open(full, "w") as f:
        json.dump({"words": words, "duration": duration}, f)
    return words, duration


def find_silences(mp3_path, work_dir, min_dur=1.5):
    """Return list of (silence_start, silence_end) via ffmpeg silencedetect."""
    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", mp3_path, "-af",
         f"silencedetect=noise=-35dB:d={min_dur}", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    silences = []
    starts = re.findall(r"silence_start: ([\d.]+)", out)
    ends = re.findall(r"silence_end: ([\d.]+)", out)
    for s, e in zip(starts, ends):
        silences.append((float(s), float(e)))
    return silences


def norm_words(words):
    """Whitespace-normalized lowercase words for matching (indices align)."""
    return [re.sub(r"\s+", " ", w[2]).strip().lower() for w in words]


def find_mentions(nw, variants):
    """Word indices mentioning a brand.

    Handles 'bit warden' -> 'bitwarden' (split words) and whisper
    mis-transcriptions like 'threadlocker' -> 'threatlocker' (fuzzy).
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
                    # Fuzzy mangling ('threadlocker' -> 'threatlocker',
                    # 0.92) is welcome, but a token that merely STARTS
                    # with the brand plus a non-plural suffix is a
                    # different word, not a mangling: 'factory' is not
                    # Factor (0.92), 'progressively' is not Progressive,
                    # and 'need' is not Indeed (0.80, below threshold).
                    # Trailing punctuation does not count as a suffix.
                    if w.startswith(v):
                        suf = w[len(v):].strip(".,!?;:")
                        if suf not in ("s", "es", "'s", ""):
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
        n = len(toks)
        for i in range(L - n + 1):
            if all(toks[j] in nw[i + j] for j in range(n)):
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
        if any(cue in " ".join(nw[j:j + 7]) for cue in OPEN_CUES):
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
            return p_start
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


def detect_ad_blocks(words, sponsors, silences, mp3_path=None, chapters=None):
    """Locate ad-read blocks via multi-method heat map (2026-10-09).

    Each detection method runs independently on the unedited episode,
    then results are cross-checked via a per-second vote heat map.
    Segments with 2+ method votes are high-confidence ads.
    Single-method hits are kept only if that method is high-precision.

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

    Returns (blocks, suspects). Suspects are low-confidence candidates
    (voice, cluster-popular, brand-orphan, reverb) that did NOT become marked blocks:
    plausible ad-length segments the pipeline was unsure about. They are
    packaged into a suspect reel (2026-10-22, per the owner) for owner review of
    minutes of uncertain audio instead of the whole episode. A suspect
    must not overlap a marked block — an overhang of a marked ad is not a
    separate suspect.
    """
    # Run each method independently, collecting (start, end, label, method)
    candidates = []
    candidates.extend([(s, e, l, 'cta') for s, e, l in _method_cta(words, sponsors, silences)])
    candidates.extend([(s, e, l, 'url') for s, e, l in _method_url(words, silences)])
    candidates.extend([(s, e, l, 'brand') for s, e, l in _method_brand(words, sponsors, silences)])
    candidates.extend([(s, e, l, 'brand-orphan') for s, e, l in _method_brand_orphan(words, sponsors, silences)])
    candidates.extend([(s, e, l, 'cluster') for s, e, l in _method_cluster(words, sponsors, silences)])
    candidates.extend([(s, e, l, 'cluster-popular') for s, e, l in _method_cluster_popular(words, sponsors, silences)])
    candidates.extend([(s, e, l, 'duplicate') for s, e, l in find_duplicate_segments(words)])
    candidates.extend([(s, e, l, 'chapter') for s, e, l in _method_chapter(words, chapters, silences)])
    if mp3_path:
        candidates.extend([(s, e, l, 'voice') for s, e, l in _method_voice(words, mp3_path)])
        candidates.extend([(s, e, l, 'reverb') for s, e, l in _method_reverb(words, mp3_path)])

    if not candidates:
        return [], []

    # Spoken-URL positions (brand stem, time) for the label picker and
    # the single-cluster anchor check.
    _nw_heat = norm_words(words)
    _bare_times = [(b, words[i][0]) for b, i in find_bare_urls(_nw_heat)]

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
            return l.split(':')[-1].strip().lower().replace(' ', '')

        def _artifact(l):
            p = _part(l)
            return p in ('dry-island', 'voice-anomaly') or \
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

    # Build heat map: per-second vote counts
    duration = int(words[-1][0]) + 1
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
                entries = sum([vote_detail[s] for s in range(bs, be)], [])
                blocks.append((bs, be, f"{'/'.join(sorted(block_methods))}:{_pick_label(entries, bs, be)}"))
                log(f"  heatmap {bs:.0f}s-{be:.0f}s ({dur:.0f}s) votes={sorted(block_methods)}")
    if in_block:
        be = duration
        dur = be - bs
        if HEATMAP_MIN_AD_LEN <= dur <= MAX_AD_LEN:
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
        if merged_blocks and bs - merged_blocks[-1][1] < 20 and \
                be - merged_blocks[-1][0] <= MAX_AD_LEN and \
                merged_blocks[-1][2].split(':')[-1] == l.split(':')[-1]:
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

    # Single-method high-precision: CTA, duplicate, cluster, brand, and
    # chapter are reliable alone. Voice, cluster-popular, and brand-orphan
    # alone have too many false positives — they only count via heat-map
    # 2+ votes.
    for s, e, label, method in candidates:
        if method in ('cta', 'duplicate', 'cluster', 'brand', 'chapter'):
            dur = e - s
            if not (MIN_AD_LEN <= dur <= MAX_AD_LEN):
                continue
            # Skip if already covered by a heatmap block
            if any(s < be and e > bs for bs, be, _ in blocks):
                continue
            # Skip duplicates (same label already added)
            if any(label == l for _, _, l in blocks):
                continue
            # Single-method cluster blocks need a spoken URL for that
            # sponsor nearby (2026-10-28): dense mentions alone can be
            # a place name — 287 'rattlesnake Ridge' produced a 77s
            # false block. Brand blocks keep cue-based precision. The
            # stem match tolerates domain labels ('bitwarden.com' vs
            # stem 'bitwarden'), label components ('Club TWiT' vs stem
            # 'twit'), and Whisper stem mangling ('manscape' vs
            # 'manscaped', 0.94).
            if method == 'cluster':
                import re as _re2
                from difflib import SequenceMatcher as _SM
                _tlds = {"com", "net", "org", "io", "tv", "co", "ai",
                         "me", "tools", "app", "dev", "xyz", "online",
                         "store", "tech", "site", "cloud", "cc", "gg",
                         "life", "world", "news", "media"}
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
            blocks.append((s, e, label))
            log(f"  single-method '{label}': {method} {s:.1f}s-{e:.1f}s ({dur:.0f}s)")

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
        refined.append((s, e, l))
    blocks = refined

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
    return [(s, e, l) for s, e, l in blocks], suspects


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


def _method_cta(words, sponsors, silences):
    """Method 'cta': Generic URL/promo-code CTA detection.
    Returns [(start, end, label)] independently."""
    nw = norm_words(words)
    results = []
    for brand, url_idx in find_url_mentions(nw):
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


def _method_url(words, silences):
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
    positions = find_bare_urls(nw)
    if not positions:
        return []
    pauses = get_pauses(words, silences)
    # Group positions within 60s (a spot repeats its URL twice, and a
    # spelled-out URL yields fragment stems like 'filter' from
    # 'L-E-A-F filter .com' — the first brand in the group names it).
    groups = []
    for brand, idx in positions:
        t = words[idx][0]
        if groups and t - groups[-1][-1][1] <= 60:
            groups[-1].append((brand, t))
        else:
            groups.append([(brand, t)])
    results = []
    for g in groups:
        brand = g[0][0]
        start, end = g[0][1] - 45.0, g[-1][1] + 10.0
        for ps, pe in pauses:
            if ps >= start and ps - start <= 10.0:
                start = ps
                break
        for ps, pe in reversed(pauses):
            if pe <= end and end - pe <= 10.0:
                end = pe
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
    being silently dropped this way. Window = mention span padded 45s each
    side, snapped to pauses, bounded by MIN/MAX_AD_LEN.
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
        start, end = max(0.0, times[0] - 45.0), times[-1] + 45.0
        # snap to nearby pauses, never into neighboring content
        for ps, pe in pauses:
            if ps >= start and ps - start <= 6.0:
                start = ps
                break
        for ps, pe in reversed(pauses):
            if pe <= end and end - pe <= 6.0:
                end = pe
                break
        if not (MIN_AD_LEN <= end - start <= MAX_AD_LEN):
            continue
        results.append((start, end, f"brand-orphan:{sp['label']}"))
        log(f"  brand-orphan '{sp['label']}': {start:.1f}s-{end:.1f}s "
            f"({len(hits)} mentions, no opening cue)")
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
        return produced[0], produced[1], "produced spot"
    seg_end = find_closing(words, nw, silences, last, seg_start)
    if seg_end is None:
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
    for i in range(n - min_shingle):
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
        if not any(abs(sec - s) < 2.0 for s, _ in merged):
            merged.append((sec, title))
    return sorted(merged)


def _method_chapter(words, chapters, silences):
    """Method 'chapter': publisher-labeled ad chapters (2026-10-14).

    Chapters titled like '[Ad]', 'Ad:', 'Advertisement', 'Sponsor' are
    high-precision ad markers. Span = chapter start -> next chapter start
    (or episode end), snapped to nearby pauses for clean chapter edges.
    Returns [(start, end, label)] independently.
    """
    if not chapters:
        return []
    duration = words[-1][0]
    pauses = get_pauses(words, silences)
    results = []
    ordered = sorted(chapters)
    for i, (start, title) in enumerate(ordered):
        if not _is_ad_chapter_title(title):
            continue
        end = ordered[i + 1][0] if i + 1 < len(ordered) else duration
        # Snap to nearby pauses for clean cuts, never into neighboring
        # content: start snaps forward, end snaps backward (<=4s each).
        for ps, pe in pauses:
            if ps >= start and ps - start <= 4.0:
                start = ps
                break
        for ps, pe in reversed(pauses):
            if pe <= end and end - pe <= 4.0:
                end = pe
                break
        if end - start < 5:
            continue
        results.append((start, end, f"chapter:{title.strip()[:40]}"))
        log(f"  chapter marker '{title.strip()[:40]}': {start:.1f}s-{end:.1f}s")
    return results


def find_url_positions(nw):
    """All spoken URL positions: {word_index: brand stem}.

    Shared by find_url_mentions (CTA-gated anchors) and _method_url
    (bare URLs). NOTE: keep in sync with extract_sponsors' TLD lists. A
    missing TLD is a silent miss: SN 1101 (2026-10-22) — the spoken
    "canary.tools" URL produced no URL anchor because "tools" was not
    in this set.
    """
    tlds = {"com", "net", "org", "io", "tv", "co", "ai", "me", "tools",
            "app", "dev", "xyz", "online", "store", "tech", "site",
            "cloud", "cc", "gg", "life", "world", "news", "media"}
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


def find_bare_urls(nw):
    """Every spoken URL as [(brand, word_index)], with NO CTA-phrase
    gate (contrast find_url_mentions). Used by _method_url and by the
    single-cluster anchor check."""
    url_at = find_url_positions(nw)
    return [(url_at[i], i) for i in sorted(url_at)]


def find_url_mentions(nw):
    """Find ad-like URL/CTA mentions: [(brand, word_index)].

    An ad anchor requires BOTH a call-to-action phrase AND a nearby URL.
    This filters out URLs mentioned in normal content.
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
    return [(b, seen[b]) for b in seen]


def sponsor_root(label):
    """Distinctive word from a sponsor label, e.g. 'material.security' -> 'material'."""
    t = re.sub(r"^https?://(www\.)?", "", label.lower()).split("/")[0]
    t = re.sub(r"\.(com|net|org|io|ai|tv|co|us|security)$", "", t)
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
    for sp in sponsors:
        variants = {v for v in sp.get("variants", set()) if v and len(v) >= 4}
        if not variants:
            continue
        hits = find_mentions(nw, variants)
        if not hits:
            log(f"  WARNING: sponsor '{sp['label']}' not in audio (0 transcript mentions)")
            missed.append({"label": sp["label"], "mentions": 0})
            continue
        hit_times = [words[i][0] for i in hits]
        covered = any(s <= t <= e for t in hit_times for s, e, _ in blocks)
        if not covered:
            covered = any(sp["label"].lower() in (lbl or "").lower()
                          for _, _, lbl in blocks)
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
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", tmp_out, "-i", meta_path,
         "-map_metadata", "1", "-c", "copy", out_path], check=True)
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


def _ad_chapter_spans(blocks, sponsors):
    """Detected blocks -> chapter spans [(start_s, end_s, title)].
    Overlapping spans merge (first title wins); titles are
    'Muse Detected Ad For <name>' or plain 'Muse Detected Ad'."""
    spans = []
    for s, e, label in sorted(blocks):
        name = ad_chapter_name(label, sponsors)
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
    prev_end = None
    for a, b in zip(pts, pts[1:]):
        contiguous = (prev_end == a)
        prev_end = b
        if b - a < 0.5:
            continue
        title = ad_at(a) or base_at(a) or (base_chapters[0][1] if base_chapters else None)
        if title is None:
            continue
        # Merge only CONTIGUOUS same-title intervals (e.g. a base boundary
        # inside one ad). Two same-titled ads separated by content must
        # stay separate chapters -- merging them would swallow the content
        # between them into one long ad chapter.
        if out and out[-1][1] == title and contiguous:
            continue
        out.append((a, title))
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
    total = sum(b["seconds"] for b in blocks)
    n = len(blocks)
    lines = [
        "———————————————————",
        "",
        f"Detected ads marked as chapters: {n} ad{'s' if n != 1 else ''} "
        f"totaling {total / 60:.1f} minutes. Each ad plays in full -- "
        f"skip to the next chapter to pass it.",
        "",
    ]
    for b in blocks:
        title = b.get("chapter") or b.get("sponsor", "")
        short = title.replace("Muse Detected Ad For ", "")
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
    publisher audio. No bonus items are emitted: QA reels and unedited
    audit items were removed 2026-10-28 (suspect reels are release assets
    only, never feed items)."""
    base = state.get("base_url", "").rstrip("/")
    marked = {g: ep for g, ep in state["processed"].items()
              if not ep.get("skipped") and ("file" in ep or "enclosure_url" in ep)}

    def repl_item(m):
        body = m.group(1)
        gm = re.search(r"<guid[^>]*>(.*?)</guid>", body, re.S)
        guid = unescape(gm.group(1)).strip() if gm else ""
        # Strip CDATA wrapper if present (html.unescape doesn't handle it)
        if guid.startswith("<![CDATA[") and guid.endswith("]]>"):
            guid = guid[9:-3].strip()
        ep = marked.get(guid)
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
        body = re.sub(
            r'<enclosure[^>]*>',
            f'<enclosure url="{new_url}" length="{ep.get("enclosure_bytes", ep["bytes"])}" type="audio/mpeg"/>',
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


def process_episode(item, state):
    slug = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")[:60]
    work = os.path.join(EP_DIR, slug)
    os.makedirs(work, exist_ok=True)
    mp3_path = os.path.join(work, "original.mp3")
    download(item["audio_url"], mp3_path)

    log(f"transcribing {item['title']}")
    words, duration = transcribe_chunked(mp3_path, work)
    log(f"transcript: {len(words)} words, {duration:.0f}s audio")

    sponsors = extract_sponsors(item["description"])
    sponsors += [dict(p) for p in HOUSE_PLUGS]
    log(f"sponsors from RSS: {[s['label'] for s in sponsors]}")
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
    blocks, suspects = detect_ad_blocks(words, sponsors, silences, mp3_path, chapters)
    missed = check_missed_sponsors(words, sponsors, blocks)
    if not sponsors:
        log("no sponsors listed in description; sponsor-independent detection only")

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
    ad_spans = _ad_chapter_spans(blocks, sponsors)
    marked_chapters = insert_ad_chapters(base, ad_spans, duration)
    out_name = f"{slug}-marked.mp3"
    out_path = os.path.join(SITE_DIR, out_name)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mp3_path,
                    "-c", "copy", out_path], check=True)
    if marked_chapters:
        write_chapters(out_path, marked_chapters, duration)
        log(f"marked {len(blocks)} ad chapters in {out_name}")
    else:
        log("no confident ad blocks; publishing whole with original chapters")
    marked_dur = probe_duration(out_path)
    # Build suspect reel (2026-10-22, per the owner): segments the pipeline
    # was unsure about, pasted chronologically. The owner reviews these
    # instead of the whole episode. NOT a feed item -- uploaded as a
    # release asset and linked in the nightly message.
    suspects_reel_name = None
    if suspects:
        suspects_reel_name = f"{slug}-suspects.mp3"
        suspects_reel_path = os.path.join(SITE_DIR, suspects_reel_name)
        build_segment_reel(mp3_path, suspects, suspects_reel_path, work)
        log(f"suspects-reel: {suspects_reel_name} ({len(suspects)} segments)")

    def chapter_for_block(s):
        for s0, e0, t in ad_spans:
            if s0 <= s < e0:
                return t
        return None

    state_blocks = []
    for s, e, l in blocks:
        name = ad_chapter_name(l, sponsors)
        title = chapter_for_block(s) or (
            f"Muse Detected Ad For {name}" if name else "Muse Detected Ad")
        state_blocks.append({"start": s, "end": e, "sponsor": l,
                             "seconds": e - s, "chapter": title})
    report = {
        "title": item["title"], "pubDate": item["pubDate"],
        "original_seconds": duration, "duration_seconds": marked_dur,
        "blocks": [{"start": s, "end": e, "sponsor": l} for s, e, l in blocks],
        "ad_chapters": [{"start": s, "end": e, "title": t} for s, e, t in ad_spans],
        "missed_sponsors": missed,
        "suspects": [{"start": s, "end": e, "label": l} for s, e, l in suspects],
    }
    with open(os.path.join(work, "report.json"), "w") as f:
        json.dump(report, f, indent=2)

    state["processed"][item["guid"]] = {
        "title": item["title"], "pubDate": item["pubDate"],
        "file": out_name,
        "bytes": os.path.getsize(out_path),
        "ads_marked": len(blocks),
        "ads_marked_seconds": sum(e - s for s, e, _ in blocks),
        "duration_seconds": marked_dur,
        "blocks": state_blocks,
        "missed_sponsors": [m["label"] for m in missed],
        "suspects": [{"label": l, "seconds": e - s} for s, e, l in suspects],
        "suspects_reel": suspects_reel_name,
        "suspects_reel_bytes": os.path.getsize(os.path.join(SITE_DIR, suspects_reel_name)) if suspects_reel_name else 0,
    }
    save_state(state)
    log(f"done: {item['title']} -- marked {len(blocks)} ads as chapters")
    return report


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--podcast", required=True,
                    help="podcast id from podcasts.yaml")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N newest unprocessed episodes (0 = no limit)")
    args = ap.parse_args()

    cfg, pcfg = init_podcast(args.podcast)
    log(f"podcast: {pcfg['name']} ({args.podcast})")
    state = load_state()
    xml = fetch_feed()
    items = parse_items(xml)
    log(f"feed has {len(items)} items")
    new_items = [it for it in reversed(items)
                 if it["guid"] and it["guid"] not in state["processed"] and it["audio_url"]]
    if args.limit:
        new_items = new_items[-args.limit:]
    if not new_items:
        log("no new episodes")
        return
    for item in new_items:
        log(f"processing: {item['title']}")
        try:
            process_episode(item, state)
        except Exception as exc:
            log(f"FAILED on {item['title']}: {exc!r}")
            # leave unprocessed so a later run retries
    write_feed(state, SITE_DIR, xml)
    log("feed regenerated")


if __name__ == "__main__":
    main()
