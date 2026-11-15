import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import run_pipeline as rp

def mk(text, t0, step=0.4):
    return [(t0 + i*step, t0 + i*step + 0.3, " " + w) for i, w in enumerate(text.split())]

def test_empty_transcript():
    assert rp.detect_ad_blocks([], [], []) == ([], [])

def test_single_method_overlap_is_not_two_votes():
    filler = mk("we talked about routers and cryptography all day long " * 40, 0)
    ad = mk(("this episode is brought to you by acmewidget go to acmewidget.com "
             "slash twit and use promo code twit acmewidget makes great widgets ") * 6, filler[-1][0] + 1)
    words = filler + ad + mk("anyway back to the show " * 30, ad[-1][0] + 1)
    blocks, sus = rp.detect_ad_blocks(words, [], [], None, None)
    # detect_ad_blocks returns (start, end, label, start_sharpness, end_sharpness).
    # Every heat-map block must be backed by 2+ DISTINCT methods.
    for item in blocks:
        l = item[2]
        methods = l.split(":")[0].split("/")
        if len(methods) > 1:
            assert len(set(methods)) >= 2
