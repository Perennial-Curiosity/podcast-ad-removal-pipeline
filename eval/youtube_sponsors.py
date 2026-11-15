"""Eval-only YouTube description sponsor supplement (from PR #1).

origin/main's extract_sponsors looks for RSS-style 'our sponsors' sections
and rel=sponsored links, so it finds almost no brands in YouTube
descriptions. PR #1 added this URL+CTA-window parser. Used here so both
pipeline versions under test see the same sponsor list; it is not applied
to pipeline/run_pipeline.py unless a later measured change decides to.
"""
import re


_AD_CONTEXT = re.compile(
    r"\b(code|coupon|discount|promo(tion)?al?|visit|my link|link:|free"
    r"|deal|offer|sponsor|partner|check out|get started|sign up|try it)\b",
    re.I,
)
_SKIP = (
    "instagram", "tiktok", "twitter", "x.com", "facebook", "youtube",
    "youtu.be", "patreon", "spotify", "apple.co", "apple.com", "twitch",
    "discord", "reddit", "wikipedia", "amzn", "amazon", "linktr.ee",
    "bit.ly", "tinyurl", "goo.gl", "t.co", "linkedin",
)
_TLD = (
    r"\.(com|net|org|io|ai|tv|co|us|tools|app|dev|xyz|online|store|tech|"
    r"site|cloud|me|cc|gg|life|world|news|media|agency|digital|solutions|"
    r"systems|network|security)$"
)


def supplement_youtube_sponsors(description, brands):
    """Append URL+CTA sponsors from a YouTube description onto `brands`."""
    text = re.sub(r"<[^>]+>", " ", description or "")
    brands = [dict(b) for b in (brands or [])]
    existing = {v for b in brands for v in b.get("variants", [])}
    for um in re.finditer(
            r"(?<![\w:/.-])(?:https?://)?(?:www\.)?"
            r"([a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[^\s\"']*)?)",
            text, re.I):
        domain = um.group(1).lower()
        host = domain.split("/")[0]
        if any(p in domain for p in _SKIP):
            continue
        window = text[max(0, um.start() - 150):um.end() + 150]
        if not _AD_CONTEXT.search(window):
            continue
        root = re.sub(_TLD, "", host)
        if not root or len(root) < 4:
            continue
        stem = root.split(".")[0]
        if len(stem) < 4:
            continue
        if stem in existing or root in existing:
            continue
        existing.add(stem)
        existing.add(root)
        brands.append({"label": stem, "variants": {root, stem}})
    return brands
