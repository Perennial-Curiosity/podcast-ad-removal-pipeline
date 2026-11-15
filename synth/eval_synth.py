"""Baseline the pipeline on the spliced set and on raw V4V hosts.

usage:
  eval_synth.py spliced --batch ../data/spliced/batch1 --label base [--oracle]
  eval_synth.py v4v --label base
Spliced: Whisper transcript (pipeline's transcribe_chunked, cached under
data/transcripts/spliced/<episode>), ffmpeg silences, sponsors from the
host description (realistic DAI: description does not list the inserted
ads) or with --oracle, a "Sponsors:" line listing the inserted brands.
No chapters. Scored with eval/score_segments.py.
V4V: runs detect_ad_blocks on the full, uncut host episode and reports
predicted ad time inside vs outside the cut V4V CTA segments.
"""
import argparse, json, os, re, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "eval"))
from pipe import load_pipeline
from transcribe import transcript_for
from score_segments import score_episode, aggregate, format_report

DATA = os.environ.get("PAD_DATA", os.path.join(REPO, "data"))


def oracle_description(desc, brands):
    items = []
    for b in brands:
        dom = b.lower() if re.search(r"\.[a-z]{2,}$", b.lower()) else re.sub(r"[^a-z0-9]", "", b.lower()) + ".com"
        items.append(f"{b} {dom}")
    return (desc or "") + "\n\nSponsors: " + " ; ".join(items) + "\n"


def span(b):
    return float(b[0]), float(b[1]), (b[2] if len(b) > 2 else "")


def run_detect(mod, audio, tdir, description):
    words, duration = transcript_for(mod, audio, tdir)
    sil_path = os.path.join(tdir, "silences.json")
    if os.path.exists(sil_path):
        silences = [tuple(s) for s in json.load(open(sil_path))]
    else:
        silences = mod.find_silences(audio, tdir, min_dur=0.8)
        json.dump(silences, open(sil_path, "w"))
    sponsors = mod.extract_sponsors(description or "")
    blocks, suspects = mod.detect_ad_blocks(words, sponsors, silences, audio, [])
    return [span(b) for b in blocks], [span(s) for s in suspects], sponsors, words


def cmd_spliced(args, mod):
    gt = json.load(open(os.path.join(args.batch, "gt.json")))
    preds, results = {}, []
    for eid in sorted(gt):
        meta = json.load(open(os.path.join(args.batch, eid, "meta.json")))
        desc = meta["description"]
        if args.oracle:
            desc = oracle_description(desc, meta["sponsors_oracle"])
        t0 = time.time()
        blocks, suspects, sponsors, words = run_detect(
            mod, os.path.join(args.batch, eid, "audio.mp3"),
            os.path.join(DATA, "transcripts", "spliced", eid), desc)
        g = [(a[0], a[1]) for a in gt[eid]["ads"]]
        r = score_episode([(s, e) for s, e, _ in blocks], g)
        r["episode"] = eid
        results.append(r)
        preds[eid] = {"blocks": [[round(s, 2), round(e, 2), l] for s, e, l in blocks],
                      "suspects": [[round(s, 2), round(e, 2), l] for s, e, l in suspects],
                      "sponsors": [s["label"] for s in sponsors]}
        print(f"{eid}: gt {len(g)} pred {len(blocks)} tp {r['tp']} ({time.time()-t0:.0f}s) "
              + ", ".join(f"{s:.0f}-{e:.0f}" for s, e, _ in blocks), flush=True)
    agg = aggregate(results)
    out = {"aggregate": agg, "episodes": results,
           "pipeline": {"path": mod._pad_source, "commit": mod._pad_commit}, "oracle": args.oracle}
    os.makedirs(os.path.join(args.batch, "results"), exist_ok=True)
    base = os.path.join(args.batch, "results", args.label)
    json.dump(preds, open(base + "_predictions.json", "w"), indent=1)
    json.dump(out, open(base + "_metrics.json", "w"), indent=1, default=str)
    rep = format_report(out, title=f"Synthetic splice set — {args.label}")
    open(base + "_report.md", "w").write(rep)
    print(rep[:3000])


def cmd_v4v(args, mod):
    manifest = json.load(open(os.path.join(DATA, "hosts", "manifest.json")))
    rows, all_preds = [], {}
    tot = {"cta_time": 0, "clean_time": 0, "pred_in_cta": 0, "pred_in_clean": 0,
           "blocks_touching_cta": 0, "blocks_clean_only": 0, "cta_segs": 0, "cta_segs_hit": 0}
    for h in manifest:
        plan_p = os.path.join(DATA, "hosts_cut", h["id"], "cut_plan.json")
        if not os.path.exists(plan_p):
            continue
        plan = json.load(open(plan_p))
        cuts = [(c["start"], c["end"]) for c in plan["cuts"]]
        blocks, suspects, sponsors, words = run_detect(
            mod, h["path"], os.path.join(DATA, "transcripts", "hosts", h["id"]), h["description"])
        def ov(a, b):
            return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
        in_cta = sum(ov((s, e), c) for s, e, _ in blocks for c in cuts)
        total = sum(e - s for s, e, _ in blocks)
        touching = sum(1 for s, e, _ in blocks if any(ov((s, e), c) > 0 for c in cuts))
        hit = sum(1 for c in cuts if any(ov((s, e), c) > 0 for s, e, _ in blocks))
        cta_time = sum(e - s for s, e in cuts)
        row = {"host": h["id"], "duration": plan["duration"], "cta_time": cta_time,
               "n_blocks": len(blocks), "pred_time": round(total, 1), "pred_in_cta": round(in_cta, 1),
               "pred_in_clean": round(total - in_cta, 1), "blocks_touching_cta": touching,
               "cta_segs": len(cuts), "cta_segs_hit": hit,
               "blocks": [[round(s, 1), round(e, 1), l] for s, e, l in blocks],
               "sponsors": [s["label"] for s in sponsors]}
        rows.append(row)
        for k, v in (("cta_time", cta_time), ("clean_time", plan["duration"] - cta_time),
                     ("pred_in_cta", in_cta), ("pred_in_clean", total - in_cta),
                     ("blocks_touching_cta", touching), ("blocks_clean_only", len(blocks) - touching),
                     ("cta_segs", len(cuts)), ("cta_segs_hit", hit)):
            tot[k] += v
        print(h["id"], {k: row[k] for k in ("n_blocks", "pred_in_cta", "pred_in_clean", "cta_segs", "cta_segs_hit")}, row["blocks"], flush=True)
    tot["cta_fp_time_share"] = tot["pred_in_cta"] / tot["cta_time"] if tot["cta_time"] else None
    tot["clean_fp_time_share"] = tot["pred_in_clean"] / tot["clean_time"] if tot["clean_time"] else None
    tot["clean_fp_blocks_per_hour"] = tot["blocks_clean_only"] / (tot["clean_time"] / 3600) if tot["clean_time"] else None
    out = {"totals": tot, "hosts": rows, "pipeline": {"path": mod._pad_source, "commit": mod._pad_commit}}
    os.makedirs(os.path.join(DATA, "v4v_cta", "results"), exist_ok=True)
    json.dump(out, open(os.path.join(DATA, "v4v_cta", "results", f"{args.label}_v4v_fp.json"), "w"), indent=1)
    print(json.dumps(tot, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["spliced", "v4v"])
    ap.add_argument("--batch", default=os.path.join(DATA, "spliced", "batch1"))
    ap.add_argument("--label", default="base")
    ap.add_argument("--oracle", action="store_true")
    args = ap.parse_args()
    mod = load_pipeline()
    mod.log = lambda m: None
    (cmd_spliced if args.mode == "spliced" else cmd_v4v)(args, mod)


if __name__ == "__main__":
    main()
