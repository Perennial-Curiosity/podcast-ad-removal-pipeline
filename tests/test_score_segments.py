import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
import score_segments as sc

def test_edge_metrics_basic():
    # pred starts 2s late, ends 1s early
    m = sc.edge_metrics([((12, 49), (10, 50), 0.8)])
    assert m["n_edges"] == 2
    assert m["mean_abs_start"] == 2
    assert m["mean_abs_end"] == 1
    assert m["median_abs"] == 1.5
    assert abs(m["signed_mean_start"] - 2) < 1e-9
    assert abs(m["signed_mean_end"] - (-1)) < 1e-9
    assert m["within_1s"] == 0.5
    assert m["within_2s"] == 1.0

def test_coverage_match_allows_wide_pred():
    # long pred covering a short GT: IoU 30/80=0.375 fails 0.5 IoU,
    # but coverage 30/30=1.0 matches the owner's >=50% of label rule.
    preds = [(0, 80)]
    gt = [(10, 40)]
    assert sc.match_segments(preds, gt, 0.5) == []
    matches = sc.match_segments_coverage(preds, gt, 0.5)
    assert len(matches) == 1

def test_score_episode_includes_edges_and_coverage():
    r = sc.score_episode([(10, 60)], [(12, 55)])
    assert r["coverage"]["tp"] == 1
    assert r["edges"]["n_edges"] == 2
    assert r["edges"]["mean_abs_start"] == 2
    assert r["edges"]["mean_abs_end"] == 5

def test_aggregate_pools_edges():
    r1 = sc.score_episode([(0, 50)], [(0, 50)])
    r1["episode"] = "a"
    r2 = sc.score_episode([(10, 60)], [(0, 50)])
    r2["episode"] = "b"
    agg = sc.aggregate([r1, r2])
    assert agg["edges"]["n_edges"] == 4
    assert agg["coverage"]["tp"] == 2
