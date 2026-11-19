import csv
import io
import json
import statistics as stx
import sys
import zipfile
import datetime as dt
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis as A
import study
from job import Job, DATA
from nse import parse_date

OI_SESSIONS = 60
TOPN = 10


def fo_oi(job, d):
    return job.n.fo_oi(d)


def run(progress=print):
    job = Job(progress=progress)
    job.trading_days()
    stocks = study.load(job, progress)
    ind = job.n.industries()
    ind_of = {s: ind.get(s, {}).get("industry", "Other") for s in stocks}
    bars_by = {s: {b["d"]: (i, b) for i, b in enumerate(x["bars"])} for s, x in stocks.items()}
    dates = sorted({b["d"] for x in stocks.values() for b in x["bars"] if b["d"] >= "2026-03-01"})
    oi_dates = set(dates[-(OI_SESSIONS + 1):])
    overnight = defaultdict(set)
    for s, x in stocks.items():
        bars = x["bars"]
        for q in x["filings"]:
            ri = A.reaction_index(bars, dt.datetime.fromisoformat(q["when"]))
            if ri is not None:
                overnight[(s, bars[ri]["d"])].add("results")
        for a in x["anns"]:
            w = parse_date(a.get("an_dt"))
            if not w or A.is_routine(a.get("desc")):
                continue
            sent, strength, _ = A.classify(a.get("desc"), a.get("attchmntText"))
            if sent == "neutral":
                continue
            ri = A.reaction_index(bars, w)
            if ri is not None:
                overnight[(s, bars[ri]["d"])].add(f"news {sent}")

    rows = []
    for k in range(25, len(dates) - 1):
        d, d1 = dates[k], dates[k + 1]
        prog = k % 20 == 0
        if prog:
            progress(f"day study {d}")
        today, nxt = {}, {}
        for s in stocks:
            a, b = bars_by[s].get(d), bars_by[s].get(d1)
            if a and b and a[0] >= 21:
                today[s], nxt[s] = a, b
        if len(today) < 100:
            continue
        r_today = {s: b["c"] / b["pc"] - 1 for s, (_, b) in today.items()}
        r_next = {s: b["c"] / b["pc"] - 1 for s, (_, b) in nxt.items()}
        order = sorted(r_next, key=r_next.get, reverse=True)
        top, bot = set(order[:TOPN]), set(order[-TOPN:])
        med = stx.median(r_today.values())
        sec = defaultdict(list)
        for s, r in r_today.items():
            sec[ind_of[s]].append(r)
        sec_med = {k_: stx.median(v) for k_, v in sec.items() if len(v) >= 3}
        oi = fo_oi(job, d) if d in oi_dates else None
        for s, (i, b) in today.items():
            bars = stocks[s]["bars"]
            hist = bars[i - 20:i]
            avgv = sum(x["v"] or 0 for x in hist) / 20
            r5 = A.chain_ret(bars, i - 4, 5)
            nb = nxt[s][1]
            f = {
                "d": d, "sym": s, "r": r_today[s] - med, "r5": r5,
                "volx": (b["v"] or 0) / avgv if avgv else None,
                "brk": b["c"] > max(x["h"] for x in hist),
                "brkdn": b["c"] < min(x["l"] for x in hist),
                "clv": (b["c"] - b["l"]) / (b["h"] - b["l"]) if b["h"] > b["l"] else 0.5,
                "sec": sec_med.get(ind_of[s]), "secn": len(sec.get(ind_of[s], [])),
                "cat": sorted(overnight.get((s, d1), ())),
                "top": s in top, "bot": s in bot,
                "nr": r_next[s], "ngap": nb["o"] / nb["pc"] - 1 if nb.get("o") else None,
                "noc": nb["c"] / nb["o"] - 1 if nb.get("o") else None,
            }
            if oi and s in oi and oi[s][0] > oi[s][1] > -oi[s][0]:
                prev_oi = oi[s][0] - oi[s][1]
                f["doi"] = oi[s][1] / prev_oi if prev_oi > 0 else None
            rows.append(f)

    def agg(g):
        n = len(g)
        if not n:
            return None
        m = lambda k: (sum(x[k] for x in g if x[k] is not None) / max(1, sum(1 for x in g if x[k] is not None)))
        return {"n": n, "top10": sum(x["top"] for x in g) / n, "bot10": sum(x["bot"] for x in g) / n,
                "next": m("nr"), "gap": m("ngap"), "open_close": m("noc")}

    base = agg(rows)
    B = {}

    def add(name, cond):
        g = [x for x in rows if cond(x)]
        if len(g) >= 30:
            B[name] = agg(g)

    add("Overnight results filing", lambda x: "results" in x["cat"])
    add("Overnight news: positive (rules)", lambda x: "news pos" in x["cat"] and "results" not in x["cat"])
    add("Overnight news: negative (rules)", lambda x: "news neg" in x["cat"] and "results" not in x["cat"])
    add("No catalyst", lambda x: not x["cat"])
    add("Today >= +5% vs market", lambda x: x["r"] >= .05)
    add("Today <= -5% vs market", lambda x: x["r"] <= -.05)
    add("Volume >= 3x, closed up", lambda x: (x["volx"] or 0) >= 3 and x["r"] > 0)
    add("Volume >= 3x, closed down", lambda x: (x["volx"] or 0) >= 3 and x["r"] < 0)
    add("20-day breakout on 2x volume", lambda x: x["brk"] and (x["volx"] or 0) >= 2)
    add("20-day breakdown on 2x volume", lambda x: x["brkdn"] and (x["volx"] or 0) >= 2)
    add("Closed at day high (top 10% of range), up", lambda x: x["clv"] >= .9 and x["r"] > .01)
    add("Sector median >= +2% today (member)", lambda x: (x["sec"] or 0) >= .02)
    add("Sector median >= +2%, stock lagged sector", lambda x: (x["sec"] or 0) >= .02 and x["r"] + 0 < (x["sec"] or 0) - .01)
    add("Sector median <= -2% today (member)", lambda x: (x["sec"] or 0) <= -.02)
    add("5-day return top (>= +10%)", lambda x: (x["r5"] or 0) >= .10)
    add("5-day return bottom (<= -10%)", lambda x: (x["r5"] or 0) <= -.10)
    oi_rows = [x for x in rows if x.get("doi") is not None]
    add("OI: long build-up (up >2%, OI +5%)", lambda x: x.get("doi") is not None and x["r"] > .02 and x["doi"] > .05)
    add("OI: short build-up (down >2%, OI +5%)", lambda x: x.get("doi") is not None and x["r"] < -.02 and x["doi"] > .05)
    add("OI: short covering (up >2%, OI -5%)", lambda x: x.get("doi") is not None and x["r"] > .02 and x["doi"] < -.05)
    add("OI: long unwinding (down >2%, OI -5%)", lambda x: x.get("doi") is not None and x["r"] < -.02 and x["doi"] < -.05)
    add("OI: big OI jump (+15%) any direction", lambda x: x.get("doi") is not None and x["doi"] > .15)
    out = {"generated": job.now.isoformat(timespec="minutes"), "sessions": len(dates) - 26,
           "rows": len(rows), "oi_rows": len(oi_rows), "base": base, "buckets": B}
    (DATA / "daystudy.json").write_text(json.dumps(out))
    return out, rows


if __name__ == "__main__":
    o, _ = run()
    pc = lambda v: " n/a " if v is None else f"{v * 100:+5.1f}%"
    b = o["base"]
    print(f"\n{o['rows']} stock-days over {o['sessions']} sessions ({o['oi_rows']} with OI). "
          f"Base: top10 {b['top10'] * 100:.1f}%  bot10 {b['bot10'] * 100:.1f}%")
    print(f"{'feature (known the evening before)':<46}{'n':>6}  top10  bot10   next   gap   open->close")
    for k, v in o["buckets"].items():
        print(f"{k:<46}{v['n']:>6}  {v['top10'] * 100:4.1f}%  {v['bot10'] * 100:4.1f}%  {pc(v['next'])} {pc(v['gap'])} {pc(v['open_close'])}")
