import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import run_pipeline as rp


def test_extract_sponsors_youtube_url_with_promo_code():
    desc = (
        "Thanks for watching! Use code ROBIN at https://www.bitbox.com/robin "
        "for a discount. Also see https://unrelated-news.org/article for the story."
    )
    brands = rp.extract_sponsors(desc)
    labels = {b["label"].lower() for b in brands}
    variants = {v for b in brands for v in b["variants"]}
    assert "bitbox" in labels or "bitbox" in variants
    # no promo-context URL should be skipped
    assert "unrelated-news" not in labels
    assert "unrelated" not in variants


def test_extract_sponsors_brought_to_you_by():
    desc = "This episode is brought to you by Acme Widget. Drink water."
    brands = rp.extract_sponsors(desc)
    variants = {v for b in brands for v in b["variants"]}
    assert "acme widget" in variants or "acmewidget" in {v.replace(" ", "") for v in variants}
    assert any("acme" in v for v in variants)


def test_extract_sponsors_sponsors_header_still_works():
    desc = "Sponsors: https://www.shopify.com/podcast and https://www.ag1.com/twit"
    brands = rp.extract_sponsors(desc)
    variants = {v for b in brands for v in b["variants"]}
    assert "shopify" in variants
    assert "ag1" in variants


def test_smashed_url_stem():
    assert rp._smashed_url_stem("xmondohaircom") == "xmondohair"
    assert rp._smashed_url_stem("mphormonesdotcom") == "mphormones"
    assert rp._smashed_url_stem("#xmondohaircom") is None  # already stripped by norm_words
    assert rp._smashed_url_stem(rp.norm_words([(0, 1, " #xmondohaircom")])[0]) == "xmondohair"
    assert rp._smashed_url_stem("welcome") is None
    assert rp._smashed_url_stem("intercom") is None
    assert rp._smashed_url_stem("paracom") is None  # stem too short
    assert rp._smashed_url_stem("online") is None


def test_smashed_url_is_cta_gated_not_a_bare_url():
    words = [
        (0, 0.3, " go"), (0.4, 0.7, " to"), (0.8, 1.1, " xmondohaircom"),
        (1.2, 1.5, " and"), (1.6, 1.9, " acmewidget"), (2.0, 2.3, " dot"),
        (2.4, 2.7, " com"),
    ]
    nw = rp.norm_words(words)
    # bare URL method must not use the smashed token (FP risk)
    assert "xmondohair" not in rp.find_url_positions(nw).values()
    assert "acmewidget" in rp.find_url_positions(nw).values()
    mentions = rp.find_url_mentions(nw)
    brands = {b for b, _ in mentions}
    assert "xmondohair" in brands
    assert "acmewidget" in brands
    # short smashed stem without a CTA must not become a bare URL
    loose = rp.norm_words([(0, 0.3, " about"), (0.4, 0.7, " paracom"), (0.8, 1.1, " today")])
    assert "para" not in rp.find_url_positions(loose).values()
    assert not any(b == "para" for b, _ in rp.find_bare_urls(loose))


def _mk(text, t0, step=0.4):
    return [(t0 + i * step, t0 + i * step + 0.3, " " + w)
            for i, w in enumerate(text.split())]

def _unique(n, t0, tag):
    vocab = ("alpha beta gamma delta epsilon zeta eta theta iota kappa "
             "lambda mu nu xi omicron pi rho sigma tau upsilon").split()
    text = " ".join(f"{tag}{i} {vocab[i % len(vocab)]}" for i in range(n))
    return _mk(text, t0)



def test_orphan_with_nearby_cta_is_promoted():
    pre = _unique(80, 0, "pre")
    ad = _mk("acmewidget makes great widgets. go to acmewidget dot com today.",
             pre[-1][0] + 1)
    post = _unique(80, ad[-1][0] + 1, "post")
    words = pre + ad + post
    sp = [{"label": "AcmeWidget", "variants": ["acmewidget"]}]
    blocks, sus = rp.detect_ad_blocks(words, sp, [], None, None)
    assert any("acmewidget" in (b[2] or "").lower() for b in blocks), (blocks, sus)
    # the promoted block should cover the mention
    mention = [w[0] for w in ad if "acmewidget" in w[2].lower()][0]
    assert any(b[0] <= mention <= b[1] for b in blocks)


def test_orphan_without_cta_stays_suspect():
    pre = _unique(80, 0, "pre")
    mid = _mk("I used an acmewidget once in a lab last year during testing.",
              pre[-1][0] + 1)
    post = _unique(80, mid[-1][0] + 1, "post")
    words = pre + mid + post
    sp = [{"label": "AcmeWidget", "variants": ["acmewidget"]}]
    blocks, sus = rp.detect_ad_blocks(words, sp, [], None, None)
    # no CTA/cue near the mention: must not become a marked block
    assert not any("acmewidget" in (b[2] or "").lower() for b in blocks), blocks


def test_extract_sponsors_youtube_bulleted_visit_line():
    """CTA on the previous bullet, URL on the next — common YT format."""
    desc = (
        "Where to store your Bitcoin? I personally use a BitBox!\n"
        "- Get a 5% Discount with code \"ROBIN\"\n"
        "- & visit: bitbox.swiss/robin\n"
        "Guest's contact: https://example-blog.org/article\n"
    )
    brands = rp.extract_sponsors(desc)
    variants = {v for b in brands for v in b["variants"]}
    assert "bitbox" in variants
    assert "example-blog" not in variants
    assert "example" not in variants


def test_extract_sponsors_visit_with_ad_tag():
    desc = (
        "Check out today's sponsors:\n\n"
        "News & Notes presented by USAA. Visit https://www.USAA.com/Insurance #ad\n"
    )
    brands = rp.extract_sponsors(desc)
    variants = {v for b in brands for v in b["variants"]}
    labels = {b["label"] for b in brands}
    assert "usaa" in variants
    assert "News" not in labels
    assert "news" not in variants


def test_extract_sponsors_does_not_claim_related_article_links():
    """'Visit Brand' after a related-article URL must not claim the article."""
    desc = (
        "Lottery winners and accident victims: is happiness relative?\n"
        "https://pubmed.ncbi.nlm.nih.gov/690806/\n"
        "\n"
        "Visit Butcher Box for this month's exclusive Mind Pump offer!\n"
        "https://www.butcherbox.com/MINDPUMP\n"
        "\n"
        "Lawmakers slam Fauci\n"
        "https://nypost.com/2021/10/24/experiments/\n"
    )
    brands = rp.extract_sponsors(desc)
    variants = {v for b in brands for v in b["variants"]}
    assert "butcherbox" in variants
    assert "pubmed" not in variants
    assert "nypost" not in variants
    assert "ncbi" not in variants


def test_extract_sponsors_affiliate_ref_query():
    desc = (
        "Get the Best Bitcoin Watches ever:\n"
        "- Here: https://www.coinvigilante.com/?ref=robinseyr\n"
        "Guest's contact: https://example-blog.org/article\n"
    )
    brands = rp.extract_sponsors(desc)
    variants = {v for b in brands for v in b["variants"]}
    assert "coinvigilante" in variants
    assert "example-blog" not in variants


def test_discover_autosponsors_from_commercial_url():
    pre = _unique(80, 0, "pre")
    ad = _mk("go to tailscale dot com slash podcast and use code POD to get started",
             pre[-1][0] + 1)
    post = _unique(80, ad[-1][0] + 1, "post")
    words = pre + ad + post
    found = rp.discover_autosponsors(words, None, [])
    variants = {v for b in found for v in b["variants"]}
    assert any(b.get("source") == "auto" for b in found)
    assert "tailscale" in variants


def test_discover_autosponsors_skips_plain_go_to_and_v4v():
    # content "go to walmart.com" with no commercial corroborator
    words = _unique(40, 0, "a") + _mk("we had to go to walmart dot com and buy milk", 200) + _unique(40, 280, "b")
    found = rp.discover_autosponsors(words, None, [])
    variants = {v for b in found for v in b["variants"]}
    assert "walmart" not in variants
    # V4V donation URL even with a slash path
    v4v = _unique(40, 0, "c") + _mk(
        "please send a donation go to noagendadonations dot com slash support thank you",
        200) + _unique(40, 280, "d")
    found = rp.discover_autosponsors(v4v, None, [])
    variants = {v for b in found for v in b["variants"]}
    assert "noagendadonations" not in variants
    assert "donations" not in variants


def test_expand_url_tail_looks_back_from_closing_url():
    words = _unique(40, 0, "p") + _mk(
        "this product is great really great go to acmewidget dot com today",
        200)
    nw = rp.norm_words(words)
    mentions = rp.find_url_mentions(nw)
    assert mentions, "expected a URL mention"
    brand, idx = mentions[0]
    seg = rp._expand_url_tail(words, [], idx)
    assert seg is not None
    s, e = seg
    assert e - s >= 30
    assert s < words[idx][0] - 10  # looks back past the URL


def test_merge_lexical_pods_joins_nearby_brand_blocks_only():
    blocks = [
        (100.0, 140.0, "cta/url:hostinger"),
        (200.0, 240.0, "reverb/url:buildkite"),
        (400.0, 450.0, "reverb/voice:dry-island"),
        (520.0, 560.0, "reverb/voice:dry-island"),
    ]
    out = rp._merge_lexical_pods(blocks, max_gap=90.0)
    # first two lexical, gap 60s, should merge
    assert any(s == 100.0 and e >= 240.0 for s, e, _ in out)
    # dry-island pair is not lexical-only in the sense... wait, labels have
    # only reverb/voice, should NOT merge
    assert any(s == 400.0 and e == 450.0 for s, e, _ in out)
    assert any(s == 520.0 and e == 560.0 for s, e, _ in out)


def test_autosponsor_helps_detect_without_show_notes():
    pre = _unique(80, 0, "pre")
    ad = _mk(
        "our friends at acmewidget made this possible. go to acmewidget dot com slash show and use code SHOW to get started today",
        pre[-1][0] + 1)
    post = _unique(80, ad[-1][0] + 1, "post")
    words = pre + ad + post
    blocks, sus = rp.detect_ad_blocks(words, [], [], None, None)
    mention = [w[0] for w in ad if "acmewidget" in w[2].lower()][0]
    assert any(b[0] <= mention <= b[1] for b in blocks), (blocks, sus)


def test_cta_tail_does_not_mark_v4v_donation_url():
    pre = _unique(80, 0, "pre")
    mid = _mk("please go to noagendadonations dot com and send a donation thank you",
              pre[-1][0] + 1)
    post = _unique(80, mid[-1][0] + 1, "post")
    words = pre + mid + post
    blocks, sus = rp.detect_ad_blocks(words, [], [], None, None)
    assert not any("donation" in (b[2] or "").lower() for b in blocks), blocks


def test_grow_fragment_walks_back_to_opening():
    pre = _unique(80, 0, "pre")
    # Produced-spot opening well before the closing URL.
    ad = _mk(
        "what is this intro. what's up nerds I am here with the ceo of fly. "
        "clouds are not magic and you can build better features. "
        "go to fly dot io slash changelog and use code SHOW to get started",
        pre[-1][0] + 1)
    post = _unique(80, ad[-1][0] + 1, "post")
    words = pre + ad + post
    nw = rp.norm_words(words)
    # A short fragment around the closing URL, like the current detector.
    url_t = [w[0] for w in ad if "fly" in w[2].lower() and w[0] > ad[0][0] + 5]
    url_t = url_t[-1]
    frag = [(url_t - 8.0, url_t + 12.0, "cta/url:fly")]
    grown = rp._grow_confirmed_fragments(words, nw, [], frag)
    assert grown[0][0] <= ad[0][0] + 6.0, grown
    assert grown[0][1] >= url_t
    # Must not eat the unique pre-roll content
    assert grown[0][0] >= pre[0][0] + 20


def test_merge_pods_when_gap_is_adlike_over_90s():
    pre = _unique(40, 0, "pre")
    a = _mk("go to hostinger dot com slash and use code practical today", 200)
    mid = _mk(
        "this episode is also brought to you by our friends at buildkite. "
        "run your CI on your own agents and scale your builds.",
        a[-1][0] + 5)
    # pad so the gap is ~120s
    pad = _mk("your pipeline stays fast with buildkite runners and agents. " * 25,
              mid[-1][0] + 1)
    b = _mk("go to buildkite dot com slash changelog and get started today",
            pad[-1][0] + 1)
    post = _unique(40, b[-1][0] + 1, "post")
    words = pre + a + mid + pad + b + post
    nw = rp.norm_words(words)
    b1 = (a[0][0], a[-1][1], "cta/url:hostinger")
    b2 = (b[0][0], b[-1][1], "cta/url:buildkite")
    gap = b2[0] - b1[1]
    assert gap > 90, gap
    out = rp._merge_lexical_pods([b1, b2], max_gap=90.0, words=words, nw=nw)
    assert len(out) == 1, (gap, out)
    assert out[0][0] == b1[0] and out[0][1] == b2[1]


def test_auto_sponsor_block_is_not_capped_to_closing_url():
    """A long read that names the brand early must stay long once marked."""
    pre = _unique(80, 0, "pre")
    body = (
        "our friends at tailscale keep your devices connected securely. "
        "with tailscale you can reach your lab from anywhere. "
    ) * 8
    cta = "go to tailscale dot com slash podcast and use code POD to get started today"
    ad = _mk(body + cta, pre[-1][0] + 1)
    post = _unique(80, ad[-1][0] + 1, "post")
    words = pre + ad + post
    first = [w[0] for w in ad if "tailscale" in w[2].lower()][0]
    last = [w[0] for w in ad if "tailscale" in w[2].lower()][-1]
    blocks, sus = rp.detect_ad_blocks(words, [], [], None, None)
    covering = [b for b in blocks if b[0] <= last <= b[1]]
    assert covering, (blocks, sus)
    # Must include the early mention, not just last-30s around the URL
    assert any(b[0] <= first + 5 for b in covering), (first, covering)


def test_v4v_donation_fragment_does_not_grow():
    pre = _unique(80, 0, "pre")
    # Donation CTA with no commercial corroborators; growing this would
    # mark thank-you / fan-mail as ads.
    body = _mk(
        "thank you to everyone who sent a donation this week. "
        "we appreciate every boostagram and every bit of support. "
        "if you want to help go to donations dot com and send value",
        pre[-1][0] + 1)
    post = _unique(80, body[-1][0] + 1, "post")
    words = pre + body + post
    nw = rp.norm_words(words)
    url_t = [w[0] for w in body if "donations" in w[2].lower()][-1]
    frag = [(url_t - 6.0, url_t + 8.0, "cta/url:donations")]
    grown = rp._grow_confirmed_fragments(words, nw, [], frag)
    # Blocked brand stem, or v4vish stop: must not walk back through thank-yous
    assert grown[0][0] >= frag[0][0] - 0.01, grown


def test_growth_without_opening_is_capped():
    """A long walk with only weak ad-like talk must not exceed ~60s."""
    pre = _unique(80, 0, "pre")
    # Brand repeats keep the walk ad-like so v5a would have gone ~150s.
    body = (
        "acmewidget helps you ship faster and your team can focus. "
    ) * 25  # ~12 words * 25 * 0.4s = ~120s
    cta = "go to acmewidget dot com slash changelog and use code SHOW to get started"
    ad = _mk(body + cta, pre[-1][0] + 1)
    post = _unique(40, ad[-1][0] + 1, "post")
    words = pre + ad + post
    nw = rp.norm_words(words)
    url_t = [w[0] for w in ad if "acmewidget" in w[2].lower()][-1]
    frag = [(url_t - 8.0, url_t + 12.0, "cta/url:acmewidget")]
    grown = rp._grow_confirmed_fragments(words, nw, [], frag)
    # Must grow some, but not all the way back through 120s of body
    assert grown[0][0] < frag[0][0] - 5, grown
    assert frag[0][0] - grown[0][0] <= rp._GROW_SOFT + 1.0, grown
    assert grown[0][0] > ad[0][0] + 20, grown


def test_growth_with_opening_can_exceed_soft_cap():
    pre = _unique(80, 0, "pre")
    pre = pre[:-1] + _mk("That was the news.", pre[-1][0])
    opener = "what's up nerds I am here with the ceo of acmewidget. "
    body = (
        "acmewidget helps you ship faster and your team can focus. "
    ) * 20  # ~80s+ of body after the opening
    cta = "go to acmewidget dot com slash changelog and use code SHOW to get started"
    ad = _mk(opener + body + cta, pre[-1][0] + 1)
    post = _unique(40, ad[-1][0] + 1, "post")
    words = pre + ad + post
    nw = rp.norm_words(words)
    url_t = [w[0] for w in ad if "acmewidget" in w[2].lower() and w[0] > ad[0][0] + 5][-1]
    frag = [(url_t - 8.0, url_t + 12.0, "cta/url:acmewidget")]
    # Opening is well more than 60s before the URL fragment
    assert frag[0][0] - ad[0][0] > rp._GROW_SOFT + 5, (frag[0][0], ad[0][0])
    grown = rp._grow_confirmed_fragments(words, nw, [], frag)
    assert grown[0][0] <= ad[0][0] + 6.0, grown
    assert grown[0][0] >= pre[0][0] + 20


def test_generic_stems_do_not_grow():
    pre = _unique(80, 0, "pre")
    body = _mk(
        "thank you to everyone who sent support this week. "
        "please go to the site and use code ITM20 for the merch shop",
        pre[-1][0] + 1)
    post = _unique(40, body[-1][0] + 1, "post")
    words = pre + body + post
    nw = rp.norm_words(words)
    t = body[-1][0] - 4
    for label in ("reverb/url:-words",
                  "brand-orphan/cta/url:brand-orphan:code-itm20"):
        frag = [(t - 6.0, t + 8.0, label)]
        grown = rp._grow_confirmed_fragments(words, nw, [], frag)
        assert grown[0][0] >= frag[0][0] - 0.01, (label, grown)


def test_glued_midrolls_split_at_return_to_show():
    pre = _unique(20, 0, "pre")
    a = _mk(
        ("hostinger keeps your sites online and you can scale today. ") * 12 +
        "go to hostinger dot com slash and use code practical today. "
        "thanks to hostinger for their support.",
        200)
    mid = _mk(
        "welcome back. " +
        "alpha beta gamma delta epsilon zeta eta theta iota kappa. " * 20,
        a[-1][0] + 1)
    b = _mk(
        "this episode is brought to you by our friends at buildkite. " +
        ("buildkite runs your CI on your own agents every day. ") * 10 +
        "go to buildkite dot com slash changelog and get started today",
        mid[-1][0] + 1)
    post = _unique(20, b[-1][0] + 1, "post")
    words = pre + a + mid + b + post
    nw = rp.norm_words(words)
    glued = [(a[0][0], b[-1][1], "cta/url:hostinger")]
    assert glued[0][1] - glued[0][0] > 90
    out = rp._split_glued_midrolls(words, nw, glued)
    assert len(out) == 2, out
    assert out[0][1] <= mid[0][0] + 15, out
    assert out[1][0] >= b[0][0] - 2, out
    # And merge must refuse the same gap
    b1 = (a[0][0], a[-1][1], "cta/url:hostinger")
    b2 = (b[0][0], b[-1][1], "cta/url:buildkite")
    merged = rp._merge_lexical_pods([b1, b2], max_gap=90.0, words=words, nw=nw)
    assert len(merged) == 2, merged
