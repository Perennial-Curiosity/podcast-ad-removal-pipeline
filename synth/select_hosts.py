"""Pick and download the host (ad-free V4V) episodes. Writes data/hosts/manifest.json."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feeds import load_feed, fetch, DATA

# (show, title substring) -- varied shows / lengths
PICKS = [
    ("noagenda", "1913 "), ("noagenda", "1911 "),
    ("pc20", "Episode 278"), ("pc20", "Episode 276"),
    ("moefactz", "97:"),
    ("doerfelverse", "Ep 81"), ("doerfelverse", "Episode 76"),
    ("argument", "7 - Ification"), ("argument", "5 - The Internet"),
    ("funfactfriday", "Episode 209"), ("funfactfriday", "Episode 205"),
    ("value4value", "Content Creation Is The Long Tail"),
    ("value4value", "What To Do When V4V"),
    ("v4vroundtable", "Roundtable 2 "),
    ("p4v", "35. Value Explained"),
    ("bowlafterbowl", "Episode 463"),
]

def main():
    out_dir = os.path.join(DATA, "hosts")
    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    feeds = {}
    for show, sub in PICKS:
        if show not in feeds:
            feeds[show] = load_feed(show)
        it = next(i for i in feeds[show]["items"] if sub in i["title"])
        hid = f"{show}-" + "".join(c for c in sub if c.isalnum())[:20].lower()
        dest = os.path.join(out_dir, hid + ".mp3")
        if not os.path.exists(dest):
            print("downloading", hid, it["url"], flush=True)
            fetch(it["url"], dest, timeout=300)
        manifest.append({"id": hid, "show": show, "feed_title": feeds[show]["title"],
                         "feed_value_tags": feeds[show]["value_tags"],
                         "title": it["title"], "pubDate": it["pubDate"],
                         "url": it["url"], "path": dest,
                         "feed_duration": it["duration"],
                         "description": it["description"][:4000]})
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print("done", len(manifest))

if __name__ == "__main__":
    main()
