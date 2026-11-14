"""Shared TLD list for URL detection across the pipeline.

Keep this in sync with extract_sponsors, find_url_positions, and
_method_cluster anchor check. A missing TLD is a silent miss:
SN 1101 (2026-10-22) — "canary.tools" kept its ".tools" suffix, so
the variant set never contained the spoken word "canary" and all
methods reported 0 mentions despite 16+ spoken mentions.
"""

TLDS = {
    "com", "net", "org", "io", "ai", "tv", "co", "us", "tools",
    "app", "dev", "xyz", "online", "store", "tech", "site", "cloud",
    "cc", "gg", "life", "world", "news", "media", "agency", "digital",
    "solutions", "systems", "network", "security",
}

# TLDs that can appear as spoken stems (e.g., "canary" from "canary.tools")
# Only for stems of 4+ chars; the common-word filter still drops generic stems.
SPOKEN_STEM_TLDS = {
    "com", "net", "org", "io", "ai", "tv", "co", "us", "tools",
    "app", "dev", "xyz", "online", "store", "tech", "site", "cloud",
    "me", "cc", "gg", "life", "world", "news", "media", "agency",
    "digital", "solutions", "systems", "network", "security",
}