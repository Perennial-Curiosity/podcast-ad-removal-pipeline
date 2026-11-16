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
    t0 = body[-1][0] - 4
    # Garbage "-words" stems are a hard block.
    frag = [(t0 - 6.0, t0 + 8.0, "reverb/url:-words")]
    grown = rp._grow_confirmed_fragments(words, nw, [], frag)
    assert grown[0][0] >= frag[0][0] - 0.01, grown


def test_code_stem_growth_is_capped_and_skips_v4v():
    """Promo-code labels may grow (unlike '-words'), within the 60s cap."""
    pre = _unique(80, 0, "pre")
    pre = pre[:-1] + _mk("That was the news.", pre[-1][0])
    body = (
        "check out betus and sign up for a free trial on your first deposit. "
    ) * 8  # ~40s of commercial talk
    cta = "go to betus dot com and use code YOUTUBE150 to get started today"
    ad = _mk(body + cta, pre[-1][0] + 1)
    post = _unique(40, ad[-1][0] + 1, "post")
    words = pre + ad + post
    nw = rp.norm_words(words)
    url_t = [w[0] for w in ad if "betus" in w[2].lower()][-1]
    frag = [(url_t - 8.0, url_t + 12.0, "brand/cta:code-youtube")]
    grown = rp._grow_confirmed_fragments(words, nw, [], frag)
    # Allowed to grow, unlike a hard-blocked '-words' stem, but capped
    assert grown[0][0] < frag[0][0] - 5, grown
    assert frag[0][0] - grown[0][0] <= rp._GROW_SOFT + 1.0, grown
    assert grown[0][0] >= pre[-1][0] - 0.5, grown


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


def test_end_refine_does_not_cut_mid_pod():
    """A promo code in the middle of a multi-brand read must not trim the end."""
    pre = _unique(80, 0, "pre")
    bitbox = (
        "our friends at bitbox made this hardware wallet for you. "
        "go to bitboxswiss slash robin and use code robin to get started. "
    )
    rest = (
        "and I partnered up with coinvigilante on a beautiful timepiece. "
        "check out those amazing coinvigilante watches today. "
    ) * 6
    ad = _mk(bitbox + rest, pre[-1][0] + 1)
    post = _unique(80, ad[-1][0] + 1, "post")
    words = pre + ad + post
    last_cv = [w[0] for w in ad if "coinvigilante" in w[2].lower()][-1]
    blocks, sus = rp.detect_ad_blocks(words, [
        {"label": "Bitbox", "variants": ["bitbox", "bitboxswiss"]},
        {"label": "CoinVigilante", "variants": ["coinvigilante"]},
    ], [], None, None)
    covering = [b for b in blocks if b[0] <= last_cv <= b[1]]
    assert covering, (blocks, sus)
    # Must include the later CoinVigilante mention, not stop at code robin
    assert any(b[1] >= last_cv - 1 for b in covering), (last_cv, covering)


def _ts_words(text, t0):
    words, t = [], float(t0)
    for w in text.split():
        words.append((t, t + 0.35, w))
        t += 0.4
    return words


def test_should_refine_end_trims_show_talk_but_keeps_mid_pod():
    """Long show-talk tails refine inward; mid-pod sales talk does not."""
    # Show talk for ~40s after the last mention.
    show = _ts_words(
        "anyway back to the story we were talking about earlier when the "
        "guests arrived and started telling us about their trip last summer "
        "and how the weather changed everything they had planned for the week "
        "and then we all laughed about the missed train " * 2,
        100.0,
    )
    ev, end = 100.0, show[-1][0]
    assert end - ev > 30.0
    assert rp._should_inward_refine_end(end, ev, show, [w[2].lower() for w in show])

    mid = _ts_words(
        "and I partnered up with coinvigilante on a beautiful timepiece. "
        "check out those amazing coinvigilante watches today at coinvigilante.com. "
        "use code robin to get started. " * 4,
        1970.0,
    )
    ev, end = 1970.0, mid[-1][0]
    assert end - ev > 30.0
    assert not rp._should_inward_refine_end(end, ev, mid, [w[2].lower() for w in mid]), (ev, end)



def test_last_adlike_end_stops_at_show_talk():
    """A commercial sentence then unique show talk should end at the ad sentence."""
    ad = _ts_words(
        "check out granola today and use code THANKS at granola.com. ",
        100.0,
    )
    # force a sentence boundary
    ad[-1] = (ad[-1][0], ad[-1][1], ad[-1][2].rstrip(",") + ".")
    gap_t = ad[-1][1] + 1.0
    show = _ts_words(
        "anyway back to the story we were talking about earlier when the "
        "guests arrived and started telling us about their trip last summer "
        "and how the weather changed everything they had planned. ",
        gap_t,
    )
    words = ad + show
    nw = [w[2].lower().strip(".,!?") for w in words]
    ev = [w[0] for w in ad if "granola.com" in w[2].lower() or "granola" in w[2].lower()][-1]
    e = show[-1][1]
    ne = rp._last_adlike_end(words, nw, ev, e)
    assert ne <= ad[-1][1] + 0.5, (ne, ad[-1][1], ev, e)
    assert e - ne > 10.0


def test_sponsoring_signoff_does_not_block_end_refine():
    """'Thank you for sponsoring' after the last mention is a sign-off, not mid-pod copy."""
    ad = _ts_words(
        "go to granola.com and use code ATP. thank you to granola for sponsoring the show. ",
        100.0,
    )
    ad[-1] = (ad[-1][0], ad[-1][1], ad[-1][2].rstrip(",") + ".")
    show = _ts_words(
        "to sustain this work we rely on listeners like you sending in boostagrams "
        "and telling a friend about the show every week. anyway back to the story. " * 4,
        ad[-1][1] + 1.0,
    )
    words = ad + show
    nw = [w[2].lower().strip(".,!?") for w in words]
    ev = [w[0] for w in ad if "granola" in w[2].lower()][-1]
    e = show[-1][1]
    assert (e - ev) > 30.0
    assert rp._should_inward_refine_end(e, ev, words, nw), (ev, e)


def test_underwrite_cues_are_opening_cues():
    for cue in (
        "this message comes from",
        "support for this podcast comes from",
        "supporters include",
        "stay tuned for the following announcements",
        "this episode sponsor is",
    ):
        assert cue in rp.OPEN_CUES
        assert cue in rp.UNDERWRITE_CUES


def test_v4v_thanks_for_support_is_not_underwrite():
    pre = _unique(20, 0, "pre")
    body = _mk(
        "thank you so much for your support. we really appreciate every donation "
        "and every boostagram this week from the community.",
        pre[-1][0] + 1,
    )
    post = _unique(20, body[-1][0] + 1, "post")
    words = pre + body + post
    hits = rp._method_underwrite(words, [])
    assert hits == [], hits


def test_underwrite_method_finds_preroll_message_comes_from():
    ad = _mk(
        "This message comes from Open Door, with cash now more later. "
        "Get cash up front for your home. Start your offer at opendoor.com slash NPR. "
        "Profits calculated after fees and costs, eligibility and offer price may vary.",
        0.0,
    )
    # force a sentence end before host talk
    ad[-1] = (ad[-1][0], ad[-1][1], ad[-1][2].rstrip(",") + ".")
    host = _mk(
        "Okay, I'm going to start today's episode by taking you back in history.",
        ad[-1][1] + 1.2,
    )
    words = ad + host
    hits = rp._method_underwrite(words, [(ad[-1][1], host[0][0])])
    assert hits, hits
    s, e, _ = hits[0]
    assert s <= 0.5, (s, e)
    assert e >= ad[-3][1], (s, e, ad[-1][1])
    assert e <= host[2][0] + 0.5, (s, e, host[0][0])


def test_edge_url_postroll_is_promoted():
    """A single url vote in the last 90s with commercial language is marked."""
    pre = _unique(40, 0, "show")
    # jump to near the end of a ~3 minute episode
    ad = _mk(
        "Many people searching for peptides worry about quality and trust. "
        "Discover uncompromising quality at oathpeptides dot com. "
        "Oath research where trust meets science.",
        200.0,
    )
    words = pre + ad
    assert words[-1][1] < 250
    blocks, sus = rp.detect_ad_blocks(words, [], [], None, None)
    covering = [b for b in blocks if b[0] <= ad[10][0] <= b[1]]
    assert covering, (blocks, sus)
    # should cover the URL / late body of the post-roll
    assert any(b[1] >= ad[8][1] for b in covering), covering


def test_url_groups_do_not_merge_different_brands():
    dell = _mk(
        "Shop at dell dot com for black friday laptops and powerful PCs. "
        "Upgrade your whole setup for less and visit dell dot com slash deals today.",
        0.0,
    )
    # long pause then a different brand
    gr = _mk(
        "This is the story of the one. Granger helps him stay prepared. "
        "Click Granger dot com or just stop by Granger.",
        25.0,
    )
    words = dell + gr
    silences = [(dell[-1][1], gr[0][0])]
    segs = rp._method_url(words, silences, None)
    brands = {s[2].lower().replace(" ", "") for s in segs}
    assert any("dell" in b for b in brands), segs
    assert any("granger" in b for b in brands), segs
    # Granger must not start at t=0 (the 45s lookback would have)
    gr_segs = [s for s in segs if "granger" in s[2].lower()]
    assert gr_segs, segs
    assert gr_segs[0][0] >= 18.0, gr_segs


def test_underwrite_and_host_boundary_detect_full_break():
    intro = _mk("You're listening to Up First from NPR. I'm A Martinez.", 0.0)
    intro[-1] = (intro[-1][0], intro[-1][1], intro[-1][2].rstrip(",") + ".")
    ad = _mk(
        "Stay tuned for the following announcements and messages from our sponsors. "
        "Support for NPR and the following message come from Made in Cookware. "
        "Learn more at madeincookware dot com. This message comes from Capital One. "
        "Capital One, what's in your wallet? Details at capitalone dot com.",
        intro[-1][1] + 1.5,
    )
    # Episode must be long enough for the 90s edge prior to apply.
    post = _unique(400, ad[-1][1] + 1.2, "post")
    post = _mk("Nvidia CEO Jensen Huang is in the news today. ", ad[-1][1] + 1.2) + post
    words = intro + ad + post
    silences = [(intro[-1][1], ad[0][0]), (ad[-1][1], post[0][0])]
    blocks, sus = rp.detect_ad_blocks(words, [], silences, None, None)
    covering = [b for b in blocks if b[0] <= ad[8][0] <= b[1]]
    assert covering, (blocks, sus)
    # should not swallow the host intro
    assert all(b[0] >= intro[-1][1] - 1.0 for b in covering), covering


def test_glued_edge_pods_split_at_new_opening():
    a = _mk(
        ("granger helps the team stay prepared every shift. ") * 10 +
        "click granger.com today for the ones who get it done.",
        0.0,
    )
    b = _mk(
        "this episode is brought to you by apollo.io. "
        + ("apollo helps sales teams find the right buyers. ") * 12 +
        "go to apollo.io slash show to get started today.",
        a[-1][1] + 2.0,
    )
    words = a + b
    nw = rp.norm_words(words)
    # one glued block covering both
    glued = [(a[0][0], b[-1][1], "cta/url:granger")]
    out = rp._split_glued_midrolls(words, nw, glued, min_part=15.0)
    assert len(out) == 2, out
    assert out[0][1] <= b[0][0] + 1.0, out
    assert out[1][0] <= b[4][0], out


def test_midroll_multi_brand_pod_is_not_split():
    """A mid-episode Tremfya-then-Petivity style pod must stay one block."""
    pre = _unique(200, 0, "pre")  # ~80s, not enough; start the ad later
    # Place the pod around 400s in a ~700s episode so it is not an edge.
    a = _mk(
        ("tremfya helps with plaque psoriasis and you can ask your doctor. ") * 10 +
        "go to tremfya dot com slash podcast to learn more.",
        400.0,
    )
    b = _mk(
        "this message comes from petivity. "
        + ("petivity monitors your cat litter box every day. ") * 10 +
        "learn more at petivity dot com slash show.",
        a[-1][1] + 2.0,
    )
    tail = _unique(400, b[-1][1] + 1.0, "tail")
    words = pre + a + b + tail
    nw = rp.norm_words(words)
    assert words[-1][1] > 700
    glued = [(a[0][0], b[-1][1], "cta/url:tremfya")]
    out = rp._split_glued_midrolls(words, nw, glued, min_part=15.0)
    assert len(out) == 1, out
    assert out[0][0] == a[0][0] and out[0][1] == b[-1][1]


def test_underwrite_growth_walks_chained_messages():
    """NPR underwriting must grow through founder copy and later
    'this message comes from' cues in the same break."""
    intro = _mk("You're listening to Up First from NPR. I'm A Martinez.", 0.0)
    intro[-1] = (intro[-1][0], intro[-1][1], intro[-1][2].rstrip(",") + ".")
    ad = _mk(
        "Stay tuned for the following announcements and messages from our sponsors. "
        "Support for NPR and the following message come from Made in Cookware. "
        "President and co-founder Jay shares why he was inspired by his family's kitchen supply business. "
        "Growing up in a commercial kitchen really opened my eyes to bringing restaurants into your home. "
        "Learn more about Made in Cookware at madeincookware dot com. "
        "This message comes from Capital One. What's in your wallet? Terms apply. Details at capitalone dot com. "
        "This message comes from Schwab. You can invest your way with Schwab.",
        intro[-1][1] + 1.5,
    )
    tail = _unique(400, ad[-1][1] + 1.5, "news")
    words = intro + ad + tail
    nw = rp.norm_words(words)
    cue_end = [w[1] for w in ad if "cookware" in w[2].lower()][0]
    frag = [(ad[0][0], cue_end + 2.0, "underwrite/url:cookware")]
    grown = rp._grow_confirmed_fragments(words, nw, [], frag)
    assert grown[0][0] <= ad[0][0] + 0.5, grown
    assert grown[0][1] >= ad[-3][1] - 1.0, grown


def test_edge_postroll_now_at_brand_without_url():
    """URL-less produced post-roll ('now at McDonald's') must be marked."""
    body = _unique(500, 0.0, "show")
    # Force a long episode so the edge prior is armed.
    last = body[-1][1]
    if last < 200:
        body = body + _unique(200, last + 0.5, "pad")
        last = body[-1][1]
    ad = _mk(
        "Holiday drinks are now at McDonald's, like the new frosted cranberry energizer. "
        "It's red bull meets cranberry flavors, topped with cold foam and sprinkles, "
        "made to charge your holiday spirit now at McDonald's. "
        "No matter what you accomplish today, celebrate with an ice-cold Coca-Cola. "
        "Enjoy Coca-Cola!",
        last + 1.5,
    )
    words = body + ad
    # bump last word so duration > 180 and ad sits in the last 90s
    assert words[-1][1] > 180
    blocks, _ = rp.detect_ad_blocks(words, sponsors=[], silences=[])
    spans = [(s, e) for s, e, *_ in blocks]
    assert any(s <= ad[0][0] + 2 and e >= ad[-1][1] - 3 for s, e in spans), (spans, ad[0][0], ad[-1][1])


def test_underwrite_refine_keeps_chained_brand_tail():
    """Post-grow refine must not trim Squarespace after a Rosetta Stone URL."""
    intro = _unique(400, 0.0, "news")
    ad = _mk(
        "Stay tuned for the following announcements and messages from our sponsors. "
        "This message comes from Rosetta Stone offering an alternative approach to language learning. "
        "More information at rosettastone dot com. "
        "A beautifully designed Squarespace website is the difference between a hobby and a career. "
        "Squarespace, because a website makes it real. "
        "Use code DIFFERENCE for 50 percent off select plans, terms and conditions apply.",
        intro[-1][1] + 2.0,
    )
    words = intro + ad
    blocks, _ = rp.detect_ad_blocks(words, sponsors=[], silences=[])
    # One block should cover the opening cue through the terms/conditions tail.
    assert any(s <= ad[0][0] + 2 and e >= ad[-3][1] - 1 for s, e, *_ in blocks), blocks


def test_show_tip_line_is_not_an_ad():
    """A show's own 'tips at audiochuck.com' closer is not a post-roll."""
    body = _unique(500, 0.0, "story")
    if body[-1][1] < 200:
        body = body + _unique(200, body[-1][1] + 0.5, "pad")
    close = _mk(
        "We want to hear from you. Send anything you've got to our tip line at tipsataudiochuck dot com or miranda at gmail dot com. "
        "Crime Junkie is an audio chuck production.",
        body[-1][1] + 2.0,
    )
    words = body + close
    blocks, _ = rp.detect_ad_blocks(words, sponsors=[], silences=[])
    # No block should cover the tip-line closer.
    for s, e, *_ in blocks:
        assert e < close[0][0] - 1 or s > close[-1][1] + 1, blocks


def test_bare_net_is_not_commercial_but_dot_com_is():
    assert not rp._span_has_commercial(["the", "net", "it", "doesn't"])
    assert not rp._span_has_commercial(["this", "is", "net", "net", "from", "detroit"])
    assert rp._span_has_commercial(["visit", "example", "dot", "com"])
    assert rp._span_has_commercial(["go", "to", "example.com"])


def test_whats_up_theme_song_is_not_an_edge_preroll():
    """V4V intros often start 'what's up' / 'I'm here' — not a preroll."""
    intro = _mk(
        "What's up, bye bye for the atom running the show. "
        "Kick down the door, here we go. No script, no filter, no corporate line.",
        40.0,
    )
    pad = _unique(500, intro[-1][1] + 1.0, "show")
    if pad[-1][1] < 200:
        pad = pad + _unique(200, pad[-1][1] + 0.5, "pad")
    words = intro + pad
    assert words[-1][1] > 180
    blocks, _ = rp.detect_ad_blocks(words, sponsors=[], silences=[])
    for s, e, *_ in blocks:
        assert e < 40 or s > intro[-1][1] + 1, blocks


def test_look_back_is_not_a_topic_shift():
    assert not rp._span_has_shift(["let's", "look", "back", "at", "the", "notes"])
    assert rp._span_has_shift(["ok", "back", "to", "the", "topic"])
    assert rp._span_has_shift(["okay", "back", "to", "cooking"])


def test_generic_oracle_stems_are_dropped():
    desc = "Sponsors: Made made.com ; John john.com ; Hostinger hostinger.com"
    brands = rp.extract_sponsors(desc)
    variants = {v for b in brands for v in b["variants"]}
    labels = {b["label"].lower() for b in brands}
    assert "made" not in variants
    assert "john" not in variants
    assert "hostinger" in variants or any("hostinger" in l for l in labels)
    # Known short popular stems must still be kept.
    desc2 = "Sponsors: Indeed indeed.com ; Seed seed.com"
    variants2 = {v for b in rp.extract_sponsors(desc2) for v in b["variants"]}
    assert "indeed" in variants2
    assert "seed" in variants2


def test_generic_made_mentions_do_not_span_the_episode():
    """A truncated oracle brand 'Made' must not mark minutes of 'made' talk."""
    pre = _unique(200, 0.0, "pre")
    talk = _mk(
        "We made dinner and then we made a plan and made our way home. "
        "John made coffee and made a joke about the news made today.",
        pre[-1][1] + 1.0,
    )
    mid = _unique(40, talk[-1][1] + 1.0, "mid")
    more = _mk("I made this later and made sure nobody made a fuss.", mid[-1][1] + 1.0)
    tail = _unique(200, more[-1][1] + 1.0, "tail")
    words = pre + talk + mid + more + tail
    assert words[-1][1] > 180
    sponsors = [{"label": "Made", "variants": {"made"}}]
    blocks, _ = rp.detect_ad_blocks(words, sponsors=sponsors, silences=[])
    # No block should cover both 'made' clusters (they'd be minutes apart).
    for s, e, *_ in blocks:
        assert e - s < 180, blocks
        # And none should start in the first talk cluster and end in the second.
        assert not (s < mid[0][0] and e > mid[-1][1]), blocks


def test_v4v_theme_song_url_is_not_a_postroll():
    """No Agenda outros plug vorac.org after 'fighting for value'."""
    body = _unique(300, 0.0, "body")
    jingle = _mk(
        "No sponsors, no product placement. Fightin for value, fightin for value, "
        "keep it alive. The best podcast in the universe, yeah, mofo to vorac "
        "dot org slash n a. This is the best.",
        body[-1][1] + 1.0,
    )
    words = body + jingle
    assert words[-1][1] > 180
    blocks, _ = rp.detect_ad_blocks(words, sponsors=[], silences=[])
    for s, e, *_ in blocks:
        assert s < body[-1][1] - 1 or e < jingle[0][0], blocks


def test_tune_in_next_time_is_not_an_edge_postroll():
    """A guest show URL just before the host outro is not a post-roll."""
    body = _unique(300, 0.0, "body")
    outro_url = _mk(
        "You can find that at lit lightningthrashes dot com. "
        "That's pretty much Sir Libre in a nutshell.",
        body[-1][1] + 1.0,
    )
    # Host credits often sit 40–60s after a guest show URL.
    pad = _unique(80, outro_url[-1][1] + 1.0, "credits")
    sign = _mk(
        "Please tune in next time, or tune in next week for the Round Table Podcast.",
        pad[-1][1] + 1.0,
    )
    words = body + outro_url + pad + sign
    outro = outro_url
    assert words[-1][1] > 180
    blocks, _ = rp.detect_ad_blocks(words, sponsors=[], silences=[])
    for s, e, *_ in blocks:
        assert s < body[-1][1] - 1 or e < outro[0][0], blocks


def test_go_to_google_is_not_an_edge_postroll():
    """Show talk 'why go to Google?' in the last 90s is not a post-roll."""
    body = _unique(300, 0.0, "body")
    talk = _mk(
        "This is good. Free is over. Google is going to have charging people. "
        "It's broken. Who goes, why go to Google? Just ask Muse. "
        "He says tokens is a new currency.",
        body[-1][1] + 1.0,
    )
    words = body + talk
    assert words[-1][1] > 180
    assert rp._edge_commercial_is_weak(["why", "go", "to", "google"])
    assert not rp._edge_commercial_is_weak(["go", "to", "dell", "dot", "com"])
    blocks, _ = rp.detect_ad_blocks(words, sponsors=[], silences=[])
    for s, e, *_ in blocks:
        assert s < body[-1][1] - 1 or e < talk[0][0], blocks
