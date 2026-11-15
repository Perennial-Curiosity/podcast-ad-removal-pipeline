"""V4V host feeds: parse RSS, list episodes, verify podcast:value, download."""
import json, os, re, sys, urllib.request, html
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("PAD_DATA", os.path.join(os.path.dirname(HERE), "data"))
UA = {"User-Agent": "Mozilla/5.0 (pad-dataset research; contact: none)"}

# Verified 2026-11-15: each feed carries <podcast:value> (lightning) tags.
FEEDS = {
    "noagenda": "https://feeds.noagendaassets.com/noagenda.xml",
    "pc20": "https://feeds.podcastindex.org/pc20.xml",
    "moefactz": "https://mp3s.nashownotes.com/mfrss.xml",
    "v4vroundtable": "https://music.jimmyv4v.com/podcasts/v4v-roundtable/v4vroundtable-rss.xml",
    "p4v": "https://filedn.com/l0rngxCNDKAhHBs9WFEP7Dj/rss/p4vrss.xml",
    "argument": "https://argumentpod.com/rss/feed.xml",
    "funfactfriday": "https://funfactfriday.com/rss/feed.xml",
    "bowlafterbowl": "https://feed.bowlafterbowl.com/feed.xml",
    "doerfelverse": "https://www.doerfelverse.com/feeds/intothedoerfelverse.xml",
    "value4value": "https://feeds.blubrry.com/feeds/1475054.xml",
}
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
      "podcast": "https://podcastindex.org/namespace/1.0"}


def fetch(url, dest=None, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if dest is None:
            return r.read()
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        os.replace(tmp, dest)
        return dest


def _dur(s):
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = [float(p) for p in s.split(":")]
    v = 0
    for p in parts:
        v = v * 60 + p
    return v


def parse_feed(show, xml_bytes):
    root = ET.fromstring(xml_bytes)
    ch = root.find("channel")
    value_tags = len(re.findall(rb"<podcast:value[\s>]", xml_bytes))
    items = []
    for it in ch.findall("item"):
        enc = it.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        guid = (it.findtext("guid") or enc.get("url")).strip()
        items.append({
            "show": show,
            "title": html.unescape((it.findtext("title") or "").strip()),
            "pubDate": it.findtext("pubDate"),
            "guid": guid,
            "url": enc.get("url"),
            "length": int(enc.get("length") or 0),
            "duration": _dur(it.findtext("itunes:duration", namespaces=NS)),
            "description": it.findtext("description") or "",
        })
    return {"show": show, "title": ch.findtext("title"),
            "value_tags": value_tags, "items": items}


def load_feed(show, refresh=False):
    d = os.path.join(DATA, "feeds")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{show}.xml")
    if refresh or not os.path.exists(p):
        fetch(FEEDS[show], p)
    with open(p, "rb") as f:
        return parse_feed(show, f.read())


if __name__ == "__main__":
    for show in (sys.argv[1:] or FEEDS):
        fd = load_feed(show)
        print(f"== {show}: {fd['title']} value_tags={fd['value_tags']} items={len(fd['items'])}")
        for it in fd["items"][:8]:
            print(f"   {it['duration'] and round(it['duration']/60)}m {it['pubDate'][:16] if it['pubDate'] else ''} | {it['title'][:70]} | {it['url'][-45:]}")
