"""Extract real ad clips with publisher-marked boundaries.

Sources (public RSS, publisher chapter markers titled "Sponsor: <brand>"):
  - Changelog network feeds: podcast:chapters JSON
  - Accidental Tech Podcast (ATP): ID3 CHAP frames inside the MP3
Clip = [sponsor chapter start, sponsor chapter end) where end is the CHAP
end_time (ATP) or the next chapter's start (Changelog).

BLAB (YouTube) audio is NOT used: YouTube returns "Sign in to confirm
you're not a bot" / HTTP 429 to this box (2026-11-15).
"""
import io, json, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feeds import fetch, DATA

AD_FEEDS = {
    "changelog": ("https://changelog.com/podcast/feed", "chapters_json", 8),
    "practicalai": ("https://changelog.com/practicalai/feed", "chapters_json", 6),
    "jsparty": ("https://changelog.com/jsparty/feed", "chapters_json", 6),
    "atp": ("https://atp.fm/rss", "id3", 8),
}
SPONSOR_RE = re.compile(r"^\s*(sponsor|sponsored by|ad)\s*[:\-]\s*(.+)$", re.I)
# Known production style per source (publisher practice, not per-clip verified)
TYPE_HINT = {"atp": "host-read", "changelog": "mixed/unknown",
             "practicalai": "mixed/unknown", "jsparty": "mixed/unknown"}


def items_with_enclosures(xml):
    out = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        body = m.group(1)
        enc = re.search(r'<enclosure[^>]*url="([^"]+)"', body)
        if not enc:
            continue
        title = re.search(r"<title>(.*?)</title>", body, re.S)
        chap = re.search(r'<podcast:chapters[^>]*url="([^"]+)"', body)
        out.append({"title": re.sub(r"<!\[CDATA\[|\]\]>", "", title.group(1)).strip() if title else "",
                    "url": enc.group(1).replace("&amp;", "&"),
                    "chapters_url": chap.group(1).replace("&amp;", "&") if chap else None})
    return out


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def sponsor_spans_json(chap_url, duration):
    j = json.loads(fetch(chap_url))
    ch = sorted(j.get("chapters", []), key=lambda c: c.get("startTime", 0))
    spans = []
    for i, c in enumerate(ch):
        m = SPONSOR_RE.match(c.get("title") or "")
        if not m:
            continue
        st = float(c["startTime"])
        en = c.get("endTime")
        if en is None:
            en = ch[i + 1]["startTime"] if i + 1 < len(ch) else duration
        spans.append((st, float(en), m.group(2).strip(), c.get("title")))
    return spans


def sponsor_spans_id3(path, duration):
    from mutagen.id3 import ID3
    t = ID3(path)
    chs = sorted(t.getall("CHAP"), key=lambda f: f.start_time)
    spans = []
    for i, fr in enumerate(chs):
        title = str(fr.sub_frames.get("TIT2") or "")
        m = SPONSOR_RE.match(title)
        if not m:
            continue
        st = fr.start_time / 1000.0
        en = fr.end_time / 1000.0 if fr.end_time and fr.end_time != 0xFFFFFFFF else None
        nxt = chs[i + 1].start_time / 1000.0 if i + 1 < len(chs) else duration
        if not en or en <= st:
            en = nxt
        spans.append((st, en, m.group(2).strip(), title))
    return spans


def main():
    src_dir = os.path.join(DATA, "ad_sources")
    clip_dir = os.path.join(DATA, "ads", "clips")
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(clip_dir, exist_ok=True)
    clips = []
    for src, (feed, kind, n_eps) in AD_FEEDS.items():
        xml = fetch(feed).decode("utf8", "replace")
        items = [i for i in items_with_enclosures(xml)
                 if kind == "id3" or i["chapters_url"]]
        took = 0
        for it in items:
            if took >= n_eps:
                break
            slug = re.sub(r"[^a-z0-9]+", "-", it["url"].split("/")[-1].split("?")[0].lower()).strip("-")
            mp3 = os.path.join(src_dir, f"{src}-{slug}")
            if not mp3.endswith(".mp3"):
                mp3 += ".mp3"
            if not os.path.exists(mp3):
                print("download", src, it["title"][:60], flush=True)
                try:
                    fetch(it["url"], mp3, timeout=600)
                except Exception as e:
                    print("  fail", e); continue
            dur = probe_duration(mp3)
            try:
                spans = (sponsor_spans_id3(mp3, dur) if kind == "id3"
                         else sponsor_spans_json(it["chapters_url"], dur))
            except Exception as e:
                print("  chapters fail", e); continue
            if not spans:
                continue
            took += 1
            for k, (st, en, brand, title) in enumerate(spans):
                d = en - st
                if d < 10 or d > 300:
                    print(f"  skip {brand} dur {d:.1f}s"); continue
                cid = f"{src}-{os.path.basename(mp3)[len(src)+1:-4]}-{k}"
                wav = os.path.join(clip_dir, cid + ".wav")
                if not os.path.exists(wav):
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{st:.3f}", "-t", f"{d:.3f}",
                                    "-i", mp3, "-ar", "44100", "-ac", "1", wav], check=True)
                clips.append({"id": cid, "source": src, "source_feed": feed,
                              "episode_title": it["title"], "episode_url": it["url"],
                              "chapter_title": title, "brand": brand,
                              "src_start": round(st, 3), "src_end": round(en, 3),
                              "duration": round(probe_duration(wav), 3),
                              "type": TYPE_HINT[src], "boundary_source": f"publisher {kind} chapters",
                              "path": wav})
    with open(os.path.join(DATA, "ads", "clips.json"), "w") as f:
        json.dump(clips, f, indent=1)
    print("clips", len(clips))


if __name__ == "__main__":
    main()
