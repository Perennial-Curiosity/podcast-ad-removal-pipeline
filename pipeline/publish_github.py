#!/usr/bin/env python3
"""Publish the newest marked episode to GitHub (releases + feed).

1. Takes the oldest marked (non-skipped) episode from the podcast's
   state that still needs publishing: no enclosure yet, a feed push
   that was never verified (feed_pushed False), or blocks that changed
   since publishing (republish_needed).
2. Creates a GitHub release (tag = episode slug) and uploads the full
   original with ad chapters as a release asset (GitHub allows 2GB/asset).
   An existing release for the tag is reused, and an existing
   same-name/same-size asset is reused instead of re-uploaded.
3. Records the asset's browser_download_url in state as enclosure_url
   with feed_pushed=False (checkpoint 1), so a failure after this
   point leaves an explicit pending flag instead of a silent
   "published" claim.
4. Regenerates the feed via run_pipeline.write_feed.
5. Pushes the feed to the repo via the Contents API, re-fetches the
   served feed and verifies the enclosure is present, then records
   feed_pushed=True and clears republish_needed (checkpoint 2).

Usage: publish_github.py --podcast <id>
Prints the feed URL on success. Exit non-zero on failure.
Uses ~/workspace/skills/github/bin/gh.py for authenticated API calls.
"""
import argparse
import base64, json, os, re, subprocess, sys, tempfile, time
import urllib.request

BASE_DIR = os.path.expanduser("~/workspace/podcast-ads")
GH = os.path.expanduser("~/workspace/skills/github/bin/gh.py")

sys.path.insert(0, os.path.join(BASE_DIR, "pipeline"))
from run_pipeline import (
    write_feed, fetch_feed, load_state, save_state, init_podcast,
    state_lock,
)

# Sentinel for _update_episode_fields(): delete the key.
_POP = object()


class GhError(Exception):
    """A gh.py failure carrying the HTTP status (m15, 2026-11-19).

    status is the HTTP code parsed from gh.py's stderr
    ("HTTP <code>: ..."), or None when gh.py failed without an HTTP
    status or returned non-JSON output. Callers treat ONLY 404 as
    "missing"; anything else fails fast."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def gh(*args):
    r = subprocess.run([GH, *args], capture_output=True, text=True)
    if r.returncode != 0:
        m = re.search(r"HTTP (\d+)", r.stderr or "")
        status = int(m.group(1)) if m else None
        raise GhError(status, f"gh.py failed: {r.stderr.strip()}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise GhError(None,
                      f"gh.py returned non-JSON output: {r.stdout[:200]!r}")


def _update_episode_fields(guid, **fields):
    """Re-load state under the state lock, apply fields, save (M28).

    A field value of _POP deletes the key. Re-loading inside the
    lock means a concurrent pipeline run's state changes are not
    clobbered by this writer's older snapshot."""
    with state_lock():
        st = load_state()
        ep = st["processed"][guid]
        for k, v in fields.items():
            if v is _POP:
                ep.pop(k, None)
            else:
                ep[k] = v
        save_state(st)


def _slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--podcast", required=True)
    args = ap.parse_args()
    cfg, pcfg = init_podcast(args.podcast)

    OWNER, REPO = cfg["github_owner"], cfg["github_repo"]
    FEED_PATH = pcfg["github_feed_path"]
    FEED_URL = (f"https://raw.githubusercontent.com/{OWNER}/{REPO}/main/{FEED_PATH}")

    # run_pipeline globals are set by init_podcast; import the module for them
    import run_pipeline as rp

    state = load_state()
    # Publishable: no enclosure yet, a feed push that was never
    # verified (C1), or blocks changed since publishing (M6).
    # Legacy entries with an enclosure and no flag are treated as
    # pushed (missing feed_pushed defaults to published).
    cands = []
    for g, e in state["processed"].items():
        if e.get("skipped") or "file" not in e:
            continue
        if (not e.get("enclosure_url")
                or e.get("feed_pushed") is False
                or e.get("republish_needed")):
            cands.append((g, e))
    guid, ep = None, None
    for g, e in cands:
        # m16a: one missing audio file no longer blocks the whole
        # queue -- log and try the next publishable episode.
        master = os.path.join(rp.SITE_DIR, e["file"])
        if not os.path.exists(master):
            print(f"WARNING: no audio file for {e['title']}; trying "
                  "the next unpublished episode", file=sys.stderr)
            continue
        guid, ep = g, e
        break
    if not ep:
        print("no marked episode in state", file=sys.stderr)
        sys.exit(1)

    slug = _slug(ep["title"])
    tag = slug[:40].strip("-") or "episode"
    # M36 (latent): warn if another episode's title produces the
    # same 40-char tag -- release reuse by tag would then be
    # ambiguous. No collision exists among the 292 live unsubscribe
    # titles (audit 2026-11-19); this only logs.
    for g2, e2 in state["processed"].items():
        if g2 != guid and (_slug(e2["title"])[:40].strip("-") or "episode") == tag:
            print(f"WARNING: tag {tag!r} collides with episode "
                  f"{e2['title']!r}; release reuse by tag is ambiguous",
                  file=sys.stderr)
    master = os.path.join(rp.SITE_DIR, ep["file"])
    local_size = os.path.getsize(master)

    # 1. Find or create the release (M36: GET-by-tag before POST,
    # so a retry after a partial publish reuses the release instead
    # of dying on GitHub's duplicate-tag 422).
    try:
        rel = gh("GET", f"/repos/{OWNER}/{REPO}/releases/tags/{tag}")
        print(f"reusing existing release {rel['id']} for tag {tag}",
              file=sys.stderr)
    except GhError as e:
        if e.status != 404:
            print(f"release lookup failed (HTTP {e.status}); not "
                  "proceeding", file=sys.stderr)
            raise SystemExit(1)
        fd, payload_path = tempfile.mkstemp(
            prefix=f"gh_rel_{args.podcast}_", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"tag_name": tag,
                           "name": f"{pcfg['name']}: {ep['title']}",
                           "body": f"Episode with {ep.get('ads_marked', 0)} "
                                   "ads marked as chapters."}, f)
            try:
                rel = gh("POST", f"/repos/{OWNER}/{REPO}/releases", payload_path)
            except GhError as e:
                print(f"release creation failed (HTTP {e.status}); not "
                      "proceeding", file=sys.stderr)
                raise SystemExit(1)
        finally:
            try:
                os.remove(payload_path)
            except OSError:
                pass
    # 2. Upload the main marked asset (reuse an existing
    # same-name/same-size asset instead of uploading a duplicate).
    assets = {a["name"]: a for a in rel.get("assets", [])}
    base = os.path.basename(master)
    if base in assets and assets[base]["size"] == local_size:
        asset = assets[base]
        print(f"reusing existing asset {base} ({local_size} bytes)",
              file=sys.stderr)
    else:
        try:
            asset = gh("UPLOAD", rel["upload_url"], master, "audio/mpeg")
        except GhError as e:
            print(f"asset upload failed (HTTP {e.status}); state "
                  "unchanged (checkpoint 1 not reached)", file=sys.stderr)
            raise SystemExit(1)
        if asset.get("size") != local_size:
            print(f"ERROR: uploaded asset size {asset.get('size')} != "
                  f"local {local_size}", file=sys.stderr)
            sys.exit(1)
    durl = asset["browser_download_url"]
    ep["enclosure_url"] = durl
    ep["enclosure_bytes"] = asset["size"]
    ep.pop("enclosure_expires_at", None)
    ep["feed_pushed"] = False
    fields = {"enclosure_url": durl, "enclosure_bytes": asset["size"],
              "feed_pushed": False, "enclosure_expires_at": _POP}
    # 2b. upload suspect reel if present (2026-10-22, per the owner).
    # Not a feed item — linked in the nightly message for his review.
    if ep.get("suspects_reel"):
        sus_path = os.path.join(rp.SITE_DIR, ep["suspects_reel"])
        if os.path.exists(sus_path):
            try:
                sus_asset = gh("UPLOAD", rel["upload_url"], sus_path, "audio/mpeg")
            except GhError as e:
                print(f"suspects upload failed (HTTP {e.status}); not "
                      "proceeding", file=sys.stderr)
                raise SystemExit(1)
            ep["suspects_url"] = sus_asset["browser_download_url"]
            ep["suspects_bytes"] = sus_asset["size"]
            fields["suspects_url"] = ep["suspects_url"]
            fields["suspects_bytes"] = ep["suspects_bytes"]
            print(f"suspects-reel uploaded: {ep['suspects_reel']}")
        else:
            # m17b: was silently skipped. The earlier suspects_url
            # (if any) is kept: the release asset from a previous
            # publish may still be live -- the URL is only definitely
            # dead after a delete (see M37 in republish289.py).
            print(f"WARNING: suspects reel missing: {sus_path}; "
                  "skipping suspects upload", file=sys.stderr)
    # Checkpoint 1 (C1): persist the enclosure with feed_pushed=False
    # BEFORE the feed regen/push, so a failure after this point
    # leaves an explicit pending flag and the next run re-selects
    # this episode instead of believing it published.
    _update_episode_fields(guid, **fields)
    # 3. regenerate feed
    xml = fetch_feed()
    feed = write_feed(state, rp.SITE_DIR, xml)
    # 4. push feed (need current SHA; create if missing). m15: only
    # a 404 means "missing"; any other gh() failure fails fast.
    try:
        cur = gh("GET", f"/repos/{OWNER}/{REPO}/contents/{FEED_PATH}")
        sha = cur["sha"]
    except GhError as e:
        if e.status == 404:
            sha = None
        else:
            print(f"feed GET failed (HTTP {e.status}); not proceeding",
                  file=sys.stderr)
            raise SystemExit(1)
    with open(os.path.join(rp.SITE_DIR, "feed.xml"), "rb") as f:
        content = base64.b64encode(f.read()).decode()
    payload = {"message": f"Update feed: {ep['title']}", "content": content}
    if sha:
        payload["sha"] = sha
    fd, payload_path = tempfile.mkstemp(
        prefix=f"gh_feed_{args.podcast}_", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        try:
            gh("PUT", f"/repos/{OWNER}/{REPO}/contents/{FEED_PATH}", payload_path)
        except GhError as e:
            print(f"feed PUT failed (HTTP {e.status}); state left at "
                  "feed_pushed=False so the next run retries", file=sys.stderr)
            raise SystemExit(1)
    finally:
        try:
            os.remove(payload_path)
        except OSError:
            pass
    # 5. Verify the pushed feed actually carries the new enclosure
    # (C1): re-GET the raw feed; a failure here leaves state at
    # checkpoint 1 (feed_pushed=False) so the next run retries.
    ok = False
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                FEED_URL, headers={"User-Agent": "podcast-ad-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                served = r.read().decode("utf-8", errors="replace")
            if durl in served:
                ok = True
                break
            print(f"pushed feed does not contain the enclosure yet "
                  f"(attempt {attempt + 1}/3)", file=sys.stderr)
        except Exception as e:
            print(f"feed verification fetch failed "
                  f"(attempt {attempt + 1}/3): {e}", file=sys.stderr)
        time.sleep(10)
    if not ok:
        print("ERROR: could not verify the pushed feed carries the new "
              "enclosure; state left at feed_pushed=False so the next "
              "run retries", file=sys.stderr)
        sys.exit(1)
    # Checkpoint 2 (C1): the push is verified; only now is the
    # episode "published".
    _update_episode_fields(guid, feed_pushed=True, republish_needed=_POP)
    print(f"feed_url={FEED_URL}")
    print(f"enclosure={durl}")


if __name__ == "__main__":
    main()
