"""Per-ad breakdown of a spliced-batch eval by ad genre and placement.
usage: breakdown.py BATCH_DIR LABEL [LABEL ...]  -> prints markdown, writes results/breakdown_<labels>.md

Per inserted ad (gt_ads.json, pods unmerged):
  hit   = predicted blocks cover >= 50% of the ad
  found = any overlap
  start edge error = (pred block start - ad start), only for the first ad in a pod
  end edge error   = (pred block end - ad end), only for the last ad in a pod
  (edge pred = the predicted block with most overlap; reported only for hit ads)
"""
import json, os, sys
from collections import defaultdict


def ov(a, b):
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def per_ad(batch, label):
    ga = json.load(open(os.path.join(batch, "gt_ads.json")))
    pr = json.load(open(os.path.join(batch, "results", f"{label}_predictions.json")))
    rows = []
    for eid, ads in ga.items():
        blocks = [(b[0], b[1]) for b in pr.get(eid, {}).get("blocks", [])]
        for a in ads:
            span = (a["start"], a["end"])
            cov = sum(ov(span, b) for b in blocks) / (span[1] - span[0])
            best = max(blocks, key=lambda b: ov(span, b), default=None)
            r = {"genre": a.get("genre", "tech"), "slot": a["slot"], "hit": cov >= 0.5,
                 "found": cov > 0, "start_err": None, "end_err": None}
            if r["hit"] and best:
                if a["pod_index"] == 0:
                    r["start_err"] = best[0] - span[0]
                if a["pod_index"] == a["pod_size"] - 1:
                    r["end_err"] = best[1] - span[1]
            rows.append(r)
    return rows


def med(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def summarize(rows):
    n = len(rows)
    hits = sum(r["hit"] for r in rows)
    found = sum(r["found"] for r in rows)
    edges = [abs(r["start_err"]) for r in rows if r["start_err"] is not None] + \
            [abs(r["end_err"]) for r in rows if r["end_err"] is not None]
    st = [r["start_err"] for r in rows if r["start_err"] is not None]
    return {"n": n, "hit": hits, "found": found, "edges": len(edges), "med": med(edges),
            "w5": (sum(e <= 5 for e in edges) / len(edges)) if edges else None,
            "signed_start": (sum(st) / len(st)) if st else None}


def fmt(s):
    p = lambda v: "n/a" if v is None else f"{100 * v:.0f}%"
    sec = lambda v: "n/a" if v is None else f"{v:.1f}s"
    sg = lambda v: "n/a" if v is None else f"{v:+.1f}s"
    return (f"{s['hit']}/{s['n']} ({p(s['hit'] / s['n'] if s['n'] else None)})", f"{s['found']}/{s['n']}",
            sec(s["med"]), p(s["w5"]), sg(s["signed_start"]), str(s["edges"]))


def table(title, groups_by_label, keys):
    labels = list(groups_by_label)
    out = [f"### {title}", "",
           "| group | " + " | ".join(f"{l}: hit (≥50%) | {l}: touched | {l}: med edge | {l}: ≤5s | {l}: signed start" for l in labels) + " |",
           "|---|" + "---|" * (5 * len(labels))]
    for k in keys:
        cells = []
        for l in labels:
            rows = groups_by_label[l].get(k, [])
            if not rows:
                cells += ["-"] * 5; continue
            f = fmt(summarize(rows))
            cells += [f[0], f[1], f"{f[2]} (n={f[5]})", f[3], f[4]]
        out.append(f"| {k} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def main():
    batch, labels = sys.argv[1], sys.argv[2:]
    by_genre, by_place, by_pool = {}, {}, {}
    for l in labels:
        rows = per_ad(batch, l)
        g, p, q = defaultdict(list), defaultdict(list), defaultdict(list)
        for r in rows:
            g[r["genre"]].append(r)
            q["tech (chapter pool)" if r["genre"] == "tech" else "non-tech (DAI pool)"].append(r)
            q["ALL"].append(r)
            p[r["slot"]].append(r)
            p["mid (any)" if r["slot"] in ("pause", "mid_sentence") else "edge-roll (pre+post)"].append(r)
        by_genre[l], by_place[l], by_pool[l] = g, p, q
    genres = sorted({k for d in by_genre.values() for k in d}, key=lambda k: (k != "tech", k))
    md = [f"# Per-ad breakdown — {os.path.basename(os.path.normpath(batch))}: {', '.join(labels)}", "",
          table("By pool", by_pool, ["ALL", "tech (chapter pool)", "non-tech (DAI pool)"]), "",
          table("By ad genre", by_genre, genres), "",
          table("By placement", by_place, ["pre", "pause", "mid_sentence", "post", "mid (any)", "edge-roll (pre+post)"])]
    txt = "\n".join(md)
    print(txt)
    open(os.path.join(batch, "results", f"breakdown_{'_'.join(labels)}.md"), "w").write(txt + "\n")


if __name__ == "__main__":
    main()
