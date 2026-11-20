#!/usr/bin/env python3
"""Publish the newest cleaned episode to the RSS feed.

1. Takes the newest cleaned (non-skipped) episode from the podcast's
   per-podcast state that has no live enclosure yet.
2. Ensures a feed MP3 under ~95MB (re-encodes the marked file at a
   lower bitrate only if needed; remote-storage rejects uploads
   >= ~100MB).
3. Uploads it via `remote-storage upload-file --ttl-hours 48`.
4. Records enclosure_url / enclosure_bytes / enclosure_expires_at in
   state.json -- only after the feed has been regenerated (C1 order:
   the enclosure is not persisted ahead of the feed that carries it).
5. Regenerates the podcast's feed-site/feed.xml from the live
   upstream feed via run_pipeline.write_feed.

The served feed is pushed by publish_github.py, never here: this
script leaves feed_pushed=False (checkpoint-1 marker) so the
episode is re-selected for the real push (T3-N1, 2026-11-20).

M27 (2026-11-19): this script could never run -- it never called
init_podcast(), so run_pipeline.load_state() raised TypeError on
STATE_PATH=None, and its own local STATE_PATH pointed at a file
that does not exist (real state is per-podcast at
podcasts/<id>/state.json). It now takes --podcast like
publish_github.py and uses run_pipeline's per-podcast paths.

Prints the enclosure URL and expiry on success. Exit non-zero on failure.
"""
import argparse, json, os, subprocess, sys, tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

BASE_DIR = os.path.expanduser("~/workspace/podcast-ads")
MAX_FEED_BYTES = 95 * 1024 * 1024  # stay under remote-storage's ~100MB cap

sys.path.insert(0, os.path.join(BASE_DIR, "pipeline"))
from run_pipeline import (
    write_feed, fetch_feed, load_state, save_state, init_podcast,
    state_lock,
)


def _pubdate_key(ep):
    """Sortable pubDate (epoch); unparseable/missing sorts first."""
    raw = ep.get("pubDate") or ""
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _enclosure_live(ep):
    """True when the episode already has a live (unexpired) enclosure
    (m16b): enclosure_url set and either no expiry recorded or the
    expiry is still in the future. An expired enclosure may be
    re-published."""
    if not ep.get("enclosure_url"):
        return False
    exp = ep.get("enclosure_expires_at")
    if not exp:
        return True
    try:
        dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except ValueError:
        return True  # unparseable expiry: treat as live, refuse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--podcast", required=True)
    args = ap.parse_args()
    cfg, pcfg = init_podcast(args.podcast)

    import run_pipeline as rp
    state = load_state()
    # m16b: newest by pubDate (not dict insertion order), excluding
    # episodes that already have a live enclosure -- re-running must
    # not mint a new 48-hour URL and orphan the old one.
    cands = [e for e in state["processed"].values()
             if not e.get("skipped") and "file" in e
             and not _enclosure_live(e)]
    if not cands:
        print("no cleaned episode in state without a live enclosure",
              file=sys.stderr)
        sys.exit(1)
    cands.sort(key=_pubdate_key)
    ep = cands[-1]
    guid = next(g for g, e in state["processed"].items() if e is ep)

    marked_path = os.path.join(rp.SITE_DIR, ep["file"])
    if not os.path.exists(marked_path):
        print(f"marked file missing: {marked_path}", file=sys.stderr)
        sys.exit(1)

    feed_path = marked_path
    tmp_feed = None
    try:
        if os.path.getsize(marked_path) > MAX_FEED_BYTES:
            # re-encode at a bitrate guaranteed to fit (m18: unique
            # temp file, removed after upload -- the old fixed
            # SITE_DIR/feed-audio.mp3 was never cleaned up).
            dur = ep.get("duration_seconds") or 3 * 3600
            bitrate = max(32000,
                          int(MAX_FEED_BYTES * 0.95 * 8 / dur / 1000) * 1000)
            fd, tmp_feed = tempfile.mkstemp(prefix="feed-audio-",
                                            suffix=".mp3", dir=rp.SITE_DIR)
            os.close(fd)
            feed_path = tmp_feed
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", marked_path,
                            "-c:a", "libmp3lame", "-b:a", f"{bitrate // 1000}k",
                            feed_path], check=True)
            if os.path.getsize(feed_path) > MAX_FEED_BYTES:
                print("re-encoded feed file still too large",
                      file=sys.stderr)
                sys.exit(1)

        out = subprocess.run(
            ["/opt/hatch/bin/remote-storage", "upload-file",
             "--path", feed_path, "--ttl-hours", "48"],
            capture_output=True, text=True)
        if out.returncode != 0:
            print(f"upload failed: {out.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        info = json.loads(out.stdout)
    finally:
        if tmp_feed:
            try:
                os.remove(tmp_feed)
            except OSError:
                pass
    ep["enclosure_url"] = info["url"]
    ep["enclosure_bytes"] = info["bytes"]
    ep["enclosure_expires_at"] = info["expires_at"]
    xml = fetch_feed()
    write_feed(state, rp.SITE_DIR, xml)
    # C1 order: save only after the feed carrying the new enclosure
    # has been regenerated. Re-load inside the lock (M28) so a
    # concurrent pipeline run's state is not clobbered.
    # T3-N1 (2026-11-20): this script performs NO feed push -- the
    # served feed is the GitHub raw feed and feed-site/ is a plain
    # directory -- so it must never claim one. feed_pushed stays
    # False (checkpoint-1 marker): the enclosure is recorded, the
    # push is unverified, and publish_github re-selects the episode,
    # pushes the served feed and verifies before setting True.
    with state_lock():
        st = load_state()
        ep2 = st["processed"][guid]
        ep2["enclosure_url"] = info["url"]
        ep2["enclosure_bytes"] = info["bytes"]
        ep2["enclosure_expires_at"] = info["expires_at"]
        ep2["feed_pushed"] = False
        ep2.pop("republish_needed", None)
        save_state(st)
    print(f"enclosure_url={info['url']}")
    print(f"expires_at={info['expires_at']}")


if __name__ == "__main__":
    main()
