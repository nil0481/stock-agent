import json
import statistics as stx
import sys
import datetime as dt
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis as A
from job import Job, DATA
from nse import parse_date

START = dt.date(2026, 5, 1)
TOPN = 10


def load(job, progress=print):
    fo = job.n.fo_symbols()
    stocks = {}
    hist_from = START - dt.timedelta(days=90)
    for i, s in enumerate(sorted(fo)):
        if i % 20 == 0:
            progress(f"study data {i}/{len(fo)}")
        try:
            fil = job.quarters_with_numbers(job.n.filings(s), 8)
            bars = job.n.history(s, hist_from)
            anns = job.n.announcements(START, job.today, symbol=s, ttl=12 * 3600)
        except RuntimeError:
            continue
        stocks[s] = {"filings": fil, "bars": bars, "anns": anns}
    return stocks


def daily_ranks(stocks):
    """date -> {sym: (ret, rank_from_top, rank_from_bottom, n)}"""
    by_d = defaultdict(dict)
    for s, x in stocks.items():
        for b in x["bars"]:
            by_d[b["d"]][s] = b["c"] / b["pc"] - 1
    out = {}
    for d, m in by_d.items():
        order = sorted(m, key=m.get, reverse=True)
        n = len(order)
        out[d] = {s: (m[s], i + 1, n - i, n) for i, s in enumerate(order)}
    return out


def measure(bars, ri, ranks):
    if ri is None or ri == 0 or ri >= len(bars):
        return None
    b = bars[ri]
    if not b.get("o"):
        return None
    rk = ranks.get(b["d"], {})
    sym_rank = None
    m = {"day": b["d"],
         "gap": b["o"] / b["pc"] - 1,
         "r1": b["c"] / b["pc"] - 1,
         "oc1": b["c"] / b["o"] - 1,
         "o5": (bars[ri + 4]["c"] / b["o"] - 1) if ri + 4 < len(bars) else None,
         "c5": A.chain_ret(bars, ri, 5)}
    return m, rk


def bucket_results(f):
    p, r = f.get("pat_yoy"), f.get("rev_yoy")
    if f.get("loss") and (f.get("pat_prev_year") or 0) > 0:
        return "turned to loss"
    if f.get("pat") and f.get("pat_prev_year") is not None and f["pat_prev_year"] <= 0 < f["pat"]:
        return "turned profitable"
    if p is None:
        return "n/a"
    if p <= -0.30:
        return "profit down >30%"
    if p <= -0.10:
        return "profit down 10-30%"
    if p < 0.10:
        return "profit flat (±10%)"
    if p < 0.30:
        return "profit up 10-30%"
    return "profit up >30%" + (" + revenue up >15%" if (r or 0) > .15 else "")


def run(progress=print):
    job = Job(progress=progress)
    job.trading_days()
    stocks = load(job, progress)
    ranks = daily_ranks(stocks)
    events = []
    for s, x in stocks.items():
        bars, fil = x["bars"], x["filings"]
        for i, q in enumerate(fil):
            when = dt.datetime.fromisoformat(q["when"])
            if when.date() < START:
                break
            f = A.quarter_facts(fil[i:i + 5])
            ri = A.reaction_index(bars, when)
            mm = measure(bars, ri, ranks)
            if not f or not mm:
                continue
            m, rk = mm
            pre = A.tech(bars[:ri])
            events.append({"sym": s, "type": "results", "when": q["when"], "bucket": bucket_results(f),
                           "pat_yoy": f.get("pat_yoy"), "rev_yoy": f.get("rev_yoy"),
                           "pre_r21": pre["r21"] if pre else None, **m,
                           "rank_top": rk.get(s, (0, 999))[1], "rank_bot": rk.get(s, (0, 999, 999))[2]})
        for a in x["anns"]:
            when = parse_date(a.get("an_dt"))
            if not when or when.date() < START or A.is_routine(a.get("desc")):
                continue
            sent, strength, kw = A.classify(a.get("desc"), a.get("attchmntText"))
            ri = A.reaction_index(bars, when)
            mm = measure(bars, ri, ranks)
            if not mm:
                continue
            m, rk = mm
            if any(abs((dt.datetime.fromisoformat(q["when"]) - when).total_seconds()) < 86400 for q in fil):
                continue
            events.append({"sym": s, "type": "news", "when": when.isoformat(), "category": a.get("desc"),
                           "rule": f"{sent}:{kw}" if sent != "neutral" else "neutral", "strength": strength,
                           **m, "rank_top": rk.get(s, (0, 999))[1], "rank_bot": rk.get(s, (0, 999, 999))[2]})

    def agg(rows):
        def mean(k):
            v = [r[k] for r in rows if r.get(k) is not None]
            return sum(v) / len(v) if v else None

        def up(k):
            v = [r[k] for r in rows if r.get(k) is not None]
            return sum(x > 0 for x in v) / len(v) if v else None
        n = len(rows)
        return {"n": n, "gap": mean("gap"), "r1": mean("r1"), "oc1": mean("oc1"), "o5": mean("o5"),
                "up_o5": up("o5"), "up_r1": up("r1"),
                "top10": sum(r["rank_top"] <= TOPN for r in rows) / n if n else None,
                "bot10": sum(r["rank_bot"] <= TOPN for r in rows) / n if n else None}

    res = [e for e in events if e["type"] == "results"]
    news = [e for e in events if e["type"] == "news"]
    by_res = defaultdict(list)
    for e in res:
        by_res[e["bucket"]].append(e)
    by_cat = defaultdict(list)
    for e in news:
        by_cat[e["category"]].append(e)
    by_rule = defaultdict(list)
    for e in news:
        by_rule[e["rule"].split(":")[0] + (":" + str(e["strength"]) if e["rule"] != "neutral" else "")].append(e)

    days = len(ranks)
    base = TOPN / (sum(len(v) for v in ranks.values()) / max(1, days))
    out = {
        "generated": job.now.isoformat(timespec="minutes"), "from": START.isoformat(), "base_top10": base,
        "n_results": len(res), "n_news": len(news),
        "results": {k: agg(v) for k, v in sorted(by_res.items())},
        "news_rule": {k: agg(v) for k, v in sorted(by_rule.items())},
        "news_category": {k: agg(v) for k, v in sorted(by_cat.items(), key=lambda kv: -len(kv[1])) if len(v) >= 8},
        "all_results": agg(res), "all_news": agg(news),
        "events": events,
    }
    (DATA / "study.json").write_text(json.dumps(out, default=str))
    return out


if __name__ == "__main__":
    o = run()
    pc = lambda v: "  n/a " if v is None else f"{v * 100:+5.1f}%"
    print(f"\nbase rate of being a top-10 F&O gainer on a given day: {o['base_top10'] * 100:.1f}%")
    for title, block in (("RESULTS (by actual profit change)", o["results"]), ("NEWS by rule", o["news_rule"]),
                         ("NEWS by NSE category (n>=8)", o["news_category"])):
        print("\n" + title)
        print(f"  {'bucket':<44}{'n':>5}  gap    r1   open->close  open->5d  up5   top10  bot10")
        for k, v in block.items():
            print(f"  {k[:44]:<44}{v['n']:>5} {pc(v['gap'])} {pc(v['r1'])} {pc(v['oc1'])}   {pc(v['o5'])} "
                  f"{pc(v['up_o5'])} {pc(v['top10'])} {pc(v['bot10'])}")
