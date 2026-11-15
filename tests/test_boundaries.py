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
