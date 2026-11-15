import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import run_pipeline as rp

def seq(text, t0, step=0.4):
    return [(round(t0 + i*step, 2), round(t0 + i*step + 0.3, 2), " " + w)
            for i, w in enumerate(text.split())]

def build():
    a = seq("so that was the router story. anyway lets move on now.", 0)
    ad = seq("this episode is brought to you by acmewidget. acmewidget makes "
             "widgets for everyone and acmewidget is great. go to acmewidget "
             "dot com today.", a[-1][1] + 1.0)
    so = seq("thanks acmewidget for supporting the show.", ad[-1][1] + 0.2)
    b = seq("ok back to the topic of cryptography and keys.", so[-1][1] + 1.0)
    return a, ad, so, b

SP = [{"label": "AcmeWidget", "variants": ["acmewidget"]}]

def test_start_pulled_to_opening_cue_and_end_to_signoff():
    a, ad, so, b = build()
    words = a + ad + so + b
    nw = rp.norm_words(words)
    # sloppy block: starts mid-read, ends before the sign-off
    blk = [(ad[8][0] + 0.1, ad[-3][0] + 0.2, "brand:AcmeWidget")]
    (s, e, _), = rp._snap_block_edges(words, nw, [], blk, SP)
    assert a[-1][1] <= s <= ad[0][0], (s, a[-1][1], ad[0][0])
    assert so[-1][1] <= e <= b[0][0], (e, so[-1][1], b[0][0])

def test_never_cuts_inside_a_word():
    a, ad, so, b = build()
    words = a + ad + so + b
    nw = rp.norm_words(words)
    (s, e, _), = rp._snap_block_edges(words, nw, [], [(ad[0][0] + 0.15, b[2][0] + 0.1, "brand:AcmeWidget")], SP)
    for w in words:
        assert not (w[0] < s < w[1]) and not (w[0] < e < w[1])

def test_never_drops_brand_mention():
    a, ad, so, b = build()
    words = a + ad + so + b
    nw = rp.norm_words(words)
    first = [w for w in ad if "acmewidget" in w[2]][0]
    (s, e, _), = rp._snap_block_edges(words, nw, [], [(first[0] - 0.05, so[-1][1], "x:AcmeWidget")], SP)
    assert s <= first[0]

def test_no_overlap_after_snap():
    a, ad, so, b = build()
    words = a + ad + so + b
    nw = rp.norm_words(words)
    out = rp._snap_block_edges(words, nw, [], [(ad[0][0], ad[10][0], "x:A"), (ad[11][0], so[-1][1], "x:B")], [])
    assert out[0][1] <= out[1][0]

def test_overshoot_without_cue_does_not_wander():
    """A block that already covers the ad, with no opening cue or
    sign-off in range, must not jump to a nearby unrelated sentence."""
    a, ad, so, b = build()
    words = a + ad + so + b
    nw = rp.norm_words(words)
    # Overshoot both edges by ~2s into adjacent content, starting on a gap.
    s = ad[0][0] - 2.0
    e = so[-1][1] + 2.0
    (ns, ne, _), = rp._snap_block_edges(words, nw, [], [(s, e, "brand:AcmeWidget")], SP)
    # Cue/sign-off may still pull inward toward the true ad, but the
    # edges must stay close to the already-wide box (no long wander).
    assert abs(ns - s) <= 6.5 and abs(ne - e) <= 6.5, (ns, ne, s, e)
    assert ns <= ad[0][0] and ne >= so[-1][1]


def test_good_friends_cue_pulls_truncated_start():
    pre = seq("we will be right back after this.", 0)
    ad = seq("our good friends at acmewidget make widgets for everyone. "
             "go to acmewidget dot com today.", pre[-1][1] + 1.0)
    words = pre + ad
    nw = rp.norm_words(words)
    # start mid-read, a few words into the sentence after the cue
    s = ad[6][0] + 0.05  # around "make"
    e = ad[-1][1]
    (ns, ne, _), = rp._snap_block_edges(words, nw, [], [(s, e, "brand:AcmeWidget")], SP)
    assert ns <= ad[0][0] + 0.2, (ns, ad[0][0], s)


def test_mid_sentence_without_cue_does_not_wander():
    """A mid-read cut with no cue or ad-ish language just outside
    is not expanded to the sentence start. (That rule helped mid-read
    but moved overshoot mean 9.32s → 10.31s; see EVAL_LOG.) Mid-word
    is still repaired."""
    pre = seq("that was a long story about the router.", 0)
    ad = seq("acmewidget makes durable widgets for home cooks and "
             "professionals everywhere you look really.", pre[-1][1] + 1.2)
    post = seq("anyway back to the show topic now.", ad[-1][1] + 1.0)
    words = pre + ad + post
    nw = rp.norm_words(words)
    s = ad[5][0] + 0.1  # inside a word, mid-sentence, no cue
    e = ad[-1][1]
    (ns, ne, _), = rp._snap_block_edges(words, nw, [], [(s, e, "x:ad")], [])
    for w in words:
        assert not (w[0] < ns < w[1])
    # must not jump back to the start of the ad/sentence
    assert ns >= s - 0.5, (ns, s)


def test_wide_overshoot_into_content_does_not_extend_outward():
    """An already-wide start that lands in host talk must not grow earlier."""
    pre = seq("today we talk about cooking pasta with garlic and oil.", 0)
    ad = seq("this episode is brought to you by acmewidget. acmewidget is great.",
             pre[-1][1] + 1.0)
    post = seq("ok back to cooking pasta now folks.", ad[-1][1] + 1.0)
    words = pre + ad + post
    nw = rp.norm_words(words)
    # start ~2s into the preceding content sentence, end past the ad
    s = ad[0][0] - 2.0
    e = ad[-1][1] + 2.0
    (ns, ne, _), = rp._snap_block_edges(words, nw, [], [(s, e, "brand:AcmeWidget")], SP)
    assert ns >= s - 0.05, (ns, s)  # no outward
    assert ns <= ad[0][0] + 0.2, (ns, ad[0][0])  # may snap inward to the cue
    assert ne <= e + 0.05, (ne, e)
    assert ne >= ad[-1][1] - 0.2, (ne, ad[-1][1])


def test_cue_lookback_cap_blocks_distant_false_cue():
    """A 'sponsor' mention 15s before an already-early start must not pull it."""
    # 0.4s/word: 15s is ~37 words of filler, then a late 'sponsor' decoy
    decoy = seq("the sponsor of last week's picnic was the city park board "
                "and then we talked for a long time about trails and maps "
                "and weather and boots and water bottles and snacks and "
                "the long walk home after dark last night.", 0)
    # gap, then content, then the real ad much later
    content = seq("today we discuss routers and keys and hashes.",
                  decoy[-1][1] + 1.0)
    ad = seq("this episode is brought to you by acmewidget today.",
             content[-1][1] + 1.0)
    words = decoy + content + ad
    nw = rp.norm_words(words)
    # already-early start: 3s before the real ad, well after the decoy cue
    s = ad[0][0] - 3.0
    e = ad[-1][1]
    assert ad[0][0] - decoy[1][0] > 12.0  # decoy 'sponsor' is far back
    (ns, ne, _), = rp._snap_block_edges(words, nw, [], [(s, e, "x:ad")], [])
    # must not jump back to the picnic 'sponsor'
    assert ns >= s - 8.5, (ns, s, decoy[1][0])
    assert ns > decoy[-1][1], (ns, decoy[-1][1])


def test_adish_language_just_past_end_extends_end():
    pre = seq("ok now a short word from a partner.", 0)
    ad = seq("acmewidget makes great widgets for everyone. go to "
             "acmewidget dot com and use code SAVE20 today.", pre[-1][1] + 1.0)
    post = seq("welcome back to the show everyone.", ad[-1][1] + 1.2)
    words = pre + ad + post
    nw = rp.norm_words(words)
    # end cuts off before the URL / promo code tail
    s = ad[0][0]
    # "use code SAVE20" sits near the end of `ad`
    cut = [w for w in ad if "use" in w[2]][0][0] - 0.05
    (ns, ne, _), = rp._snap_block_edges(words, nw, [], [(s, cut, "x:ad")], [])
    assert ne > cut + 0.3, (ne, cut)
    assert ne <= post[0][0] + 0.05, (ne, post[0][0])


def test_wide_start_snaps_inward_to_cue_not_outward():
    pre = seq("and that is how the router story ends today.", 0)
    ad = seq("this episode is brought to you by acmewidget. acmewidget rules.",
             pre[-1][1] + 1.0)
    words = pre + ad
    nw = rp.norm_words(words)
    s = pre[2][0]  # well before the cue, inside prior content
    e = ad[-1][1]
    (ns, ne, _), = rp._snap_block_edges(words, nw, [], [(s, e, "brand:AcmeWidget")], SP)
    assert ns >= s - 0.05, (ns, s)
    assert abs(ns - ad[0][0]) <= 0.5, (ns, ad[0][0])


def test_hash_prefixed_cue_is_recognized():
    """BLAB-style '#brought #to #you #by' must still pull a truncated start."""
    pre = seq("that was the story about the router today.", 0)
    ad = [(round(pre[-1][1] + 1.0 + i*0.4, 2),
           round(pre[-1][1] + 1.0 + i*0.4 + 0.3, 2),
           " #" + w)
          for i, w in enumerate("brought to you by acmewidget today everyone.".split())]
    words = pre + ad
    nw = rp.norm_words(words)
    assert nw[len(pre)] == "brought"
    s = ad[5][0] + 0.05
    e = ad[-1][1]
    (ns, ne, _), = rp._snap_block_edges(words, nw, [], [(s, e, "x:ad")], [])
    assert ns <= ad[0][0] + 0.2, (ns, ad[0][0], nw[len(pre):len(pre)+6])
