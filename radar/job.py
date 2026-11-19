import io
import json
import os
import sys
import time
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import analysis as A
import llm
import movers
import telegram
from nse import NSE, parse_date

DATA = ROOT / "data"
OUT = DATA / "radar.json"
CASE_SYMBOLS = ["KALYANKJIL", "POWERINDIA", "SIEMENS", "CGPOWER"]
SIGNAL_ORDER = ["STRONG BUY", "BUY", "BUY (RISKY)", "WATCH UP", "WATCH DOWN", "SELL", "STRONG SELL"]


def now_ist():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).replace(tzinfo=None)


class Job:
    def __init__(self, progress=None):
        self.n = NSE(DATA / "cache")
        self.progress = progress or (lambda msg: print(msg, flush=True))
        self.now = now_ist()
        self.today = self.now.date()
        self.notes = []

    def pdf_text(self, url, max_pages=3):
        if not url or not url.lower().endswith(".pdf"):
            return ""
        key = "pdf:" + url
        v = self.n._cache_get(key, None)
        if v is not None:
            return v
        try:
            from pypdf import PdfReader
            b = self.n._fetch(url, as_json=False, timeout=60)
            if len(b) > 6_000_000:
                v = ""
            else:
                rd = PdfReader(io.BytesIO(b))
                v = " ".join((p.extract_text() or "") for p in rd.pages[:max_pages])
                v = " ".join(v.split())
                i = v.lower().find("sub")
                v = v[i:] if 0 <= i < 1500 else v
                v = v[:5000]
        except Exception:
            v = ""
        self.n._cache_put(key, v)
        return v

    def trading_days(self):
        try:
            data = self.n.get_json("https://www.nseindia.com/api/holiday-master?type=trading", 7 * 86400)
            hol = {parse_date(x.get("tradingDate")).date() for x in data.get("CM", []) if parse_date(x.get("tradingDate"))}
        except Exception:
            hol = set()
        self.holidays = hol

    def next_trading_day(self, d):
        d = d + dt.timedelta(days=1)
        while d.weekday() >= 5 or d in self.holidays:
            d += dt.timedelta(days=1)
        return d

    def prev_trading_day(self, d):
        d = d - dt.timedelta(days=1)
        while d.weekday() >= 5 or d in self.holidays:
            d -= dt.timedelta(days=1)
        return d

    def news_since(self):
        """News counts if the market could react to it in the current/next session or the one before:
        from 15:30 two sessions before the reaction session (daystudy: news edge lasts about one session)."""
        session = self.today
        if session.weekday() >= 5 or session in self.holidays:
            session = self.next_trading_day(session)
        d = self.prev_trading_day(self.prev_trading_day(session))
        return dt.datetime.combine(d, dt.time(15, 30))

    def events_for(self, anns):
        evs = []
        for x in anns:
            when = parse_date(x.get("an_dt"))
            if not when:
                continue
            sent, strength, kw = A.classify(x.get("desc"), x.get("attchmntText"))
            if sent == "neutral":
                continue
            line = f"{x.get('desc')} ({when:%d %b})"
            evs.append({"when": when, "sent": sent, "strength": strength, "kw": kw, "line": line,
                        "governance": sent == "neg" and kw in A.GOVERNANCE})
        return evs

    def quarters_with_numbers(self, filings, k):
        out = []
        for q in filings[:k]:
            try:
                q = dict(q, **self.n.xbrl_numbers(q["xbrl"]))
            except Exception:
                q = dict(q)
            out.append(q)
        return out

    def run(self):
        t0 = time.time()
        llm.reset_stats()
        self.trading_days()
        self.progress("Loading F&O list")
        fo = self.n.fo_symbols()
        ind = self.n.industries()
        self.ind_of = {s: ind.get(s, {}).get("industry", "Other") for s in fo}
        hist_start = self.today - dt.timedelta(days=150)
        ann_start = self.today - dt.timedelta(days=120)
        stocks = {}
        syms = sorted(fo)
        if os.getenv("RADAR_ONLY"):
            syms = [s for s in os.environ["RADAR_ONLY"].split(",") if s in fo]
        for i, s in enumerate(syms):
            if i % 10 == 0:
                self.progress(f"Fetching exchange data {i}/{len(syms)} ({s})")
            try:
                fil = self.n.filings(s)
                bars = self.n.history(s, hist_start)
                anns = self.n.announcements(ann_start, self.today, symbol=s, ttl=6 * 3600)
            except RuntimeError as e:
                self.notes.append(f"{s}: data fetch failed ({e.__class__.__name__})")
                continue
            q = self.quarters_with_numbers(fil, 6)
            name = (ind.get(s, {}).get("name") or (fil[0]["name"] if fil else None) or fo[s]).strip()
            stocks[s] = {"sym": s, "name": name, "filings": q, "bars": bars, "anns": anns,
                         "events": self.events_for(anns)}
        self.stocks = stocks
        self._ret_cache = {}

        self.progress("Backtesting last results season")
        backtest = self.backtest()
        self.progress("Building pre-results radar")
        radar = self.radar()
        self.progress("Movers board (fresh results, live confirmation)")
        board, mv_signals, results_syms = movers.build(self)
        self.results_syms = results_syms
        self.progress("Reading recent announcements for event signals")
        ev_signals = [dict(e, kind="news") for e in self.event_signals()]
        self.progress("Case studies")
        cases = self.case_studies()

        signals = [dict(x, kind="pre-results") for x in radar.pop("signals")] + mv_signals + ev_signals
        self.progress("Live prices for signals")
        stamp = self.now.strftime("%d-%b-%Y")
        for sg in signals:
            if sg.get("live"):
                continue
            try:
                q = self.n.live_quote(sg["sym"])
            except RuntimeError:
                q = None
            if q and q.get("open") and q.get("last") and (q.get("time") or "").startswith(stamp):
                d_ = q["last"] - sg["cmp"]
                rnd = lambda v: round(round(v / 0.05) * 0.05, 2)
                sg.update(cmp=rnd(q["last"]), entry=rnd(q["last"]), target=rnd(sg["target"] + d_),
                          stop=rnd(sg["stop"] + d_), live=True, today=q["chg"])
        kept = []
        for sg in signals:
            mv = sg.get("today")
            against = mv is not None and sg.get("kind") in ("news", "follow-through") and \
                ((mv <= -0.02 and "BUY" in sg["signal"]) or (mv >= 0.02 and "SELL" in sg["signal"]))
            if against:
                self.filtered_events = getattr(self, "filtered_events", []) + [
                    f"{sg['name']}: {sg['signal']} withdrawn, stock {A.pct(mv, 1)} today against the signal"]
            else:
                kept.append(sg)
        signals = kept
        self.progress("Updating signal tracker")
        tracker, track_summary = movers.track(self, signals)
        self.progress("Likely big movers and live sector themes")
        likely = movers.likely(self)
        themes = movers.themes(self, self.ind_of)
        playbook = None
        if (DATA / "study.json").exists():
            st_ = json.loads((DATA / "study.json").read_text())
            playbook = {k: st_.get(k) for k in ("from", "base_top10", "n_results", "n_news", "results", "news_rule",
                                                 "all_results", "all_news")}
        signals.sort(key=lambda x: (SIGNAL_ORDER.index(x["signal"]) if x["signal"] in SIGNAL_ORDER else 9, x["name"]))
        prev = json.loads(OUT.read_text()) if OUT.exists() else None
        result = {
            "generated_at": self.now.strftime("%Y-%m-%d %H:%M IST"),
            "as_of_close": max((b["d"] for st_ in stocks.values() for b in st_["bars"][-1:]), default=None),
            "universe": len(fo), "loaded": len(stocks),
            **radar,
            "signals": signals,
            "movers": board,
            "likely": likely, "themes": themes,
            "tracker": tracker, "track_summary": track_summary,
            "playbook": playbook,
            "backtest": backtest,
            "cases": cases,
            "notes": self.notes,
            "filtered_events": getattr(self, "filtered_events", []),
            "llm": llm.enabled(),
            "llm_usage": dict(llm.STATS),
            "runtime_sec": round(time.time() - t0),
            "requests": self.n.requests_made,
        }
        result["changes"] = diff(prev, result)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, default=str, indent=1))
        tmp.replace(OUT)
        self.progress(f"Done in {result['runtime_sec']}s ({self.n.requests_made} exchange requests)")
        return result

    def rets_at(self, date_iso):
        if date_iso not in self._ret_cache:
            out = {}
            for s, x in self.stocks.items():
                bb = [b for b in x["bars"] if b["d"] < date_iso][-21:]
                if len(bb) == 21:
                    r = 1.0
                    for b in bb:
                        r *= b["c"] / b["pc"]
                    out[s] = r - 1
            self._ret_cache[date_iso] = out
        return self._ret_cache[date_iso]

    def assess(self, x, before: dt.datetime, bars_before, quarters):
        """Score a stock using only information available before `before`."""
        f = A.quarter_facts(quarters)
        if not f:
            return None
        t = A.tech(bars_before)
        ev90 = [e for e in x["events"] if before - dt.timedelta(days=90) <= e["when"] < before]
        ev_all = [e for e in x["events"] if e["when"] < before]
        ref = bars_before[-1]["d"] if bars_before else before.date().isoformat()
        srel = A.sector_relative(self.ind_of.get(x["sym"]), self.ind_of, self.rets_at, ref + "Z")
        pts = A.score(f, t, ev90, srel)
        bk, why = A.bucket(f, pts, ev_all)
        sig = A.signal_from(bk, pts, f)
        mcap = f["shares"] * t["close"] / 1e7 if (f.get("shares") and t) else None
        return {"f": f, "t": t, "pts": pts, "bucket": bk, "excluded": why, "signal": sig,
                "mcap_cr": mcap, "ev90": ev90, "sector_rel": srel}

    def backtest(self):
        lo = self.now - dt.timedelta(days=50)
        rows = []
        for s, x in self.stocks.items():
            fil = x["filings"]
            if len(fil) < 2:
                continue
            E = fil[0]
            when = dt.datetime.fromisoformat(E["when"])
            if when < lo:
                continue
            bars = x["bars"]
            ri = A.reaction_index(bars, when)
            prior = fil[1:6]
            a = self.assess(x, when, bars[:ri] if ri is not None else bars, prior)
            if not a:
                continue
            actual = A.quarter_facts(fil[:5])
            rec = {
                "sym": s, "name": x["name"], "filed": when.strftime("%d %b %H:%M"), "filed_iso": E["when"],
                "quarter": A.quarter_label(E["qe"]), "signal": a["signal"], "bucket": a["bucket"],
                "score": a["pts"]["total"],
                "pre_r21": a["t"]["r21"] if a["t"] else None,
                "act_rev_yoy": actual.get("rev_yoy"), "act_pat_yoy": actual.get("pat_yoy"),
                "react_day": bars[ri]["d"] if ri is not None else None,
                "r1": A.chain_ret(bars, ri, 1), "r5": A.chain_ret(bars, ri, 5),
                "r_to_date": A.chain_ret(bars, ri, len(bars) - ri) if ri is not None else None,
                "vol_x": (bars[ri]["v"] / (sum(b["v"] for b in bars[max(0, ri - 30):ri]) / max(1, len(bars[max(0, ri - 30):ri]))))
                if ri is not None and ri > 5 else None,
            }
            rows.append(rec)
        rows.sort(key=lambda r: r["filed_iso"], reverse=True)

        def summarize(group, sell=False):
            out = {"n": len(group)}
            for k in ("r1", "r5"):
                vals = [g[k] for g in group if g[k] is not None]
                out["n_" + k] = len(vals)
                out["avg_" + k] = sum(vals) / len(vals) if vals else None
                out["win_" + k] = (sum((v < 0) if sell else (v > 0) for v in vals) / len(vals)) if vals else None
            return out

        summary = []
        for sig in SIGNAL_ORDER + [None]:
            g = [r for r in rows if r["signal"] == sig]
            if g:
                summary.append({"signal": sig or "No signal", **summarize(g, sell=bool(sig and "SELL" in sig))})
        recent_from = (self.today - dt.timedelta(days=10)).isoformat()
        return {
            "window": f"results filed {lo:%d %b} to {self.now:%d %b %Y}",
            "method": "Score and signal computed only with data available before each filing; return measured from the "
                      "last close before the market could react, to the close 1 and 5 trading days later.",
            "summary": summary,
            "all_reporters": summarize(rows),
            "recent": [r for r in rows if (r["react_day"] or "9999") >= recent_from or r["filed_iso"][:10] >= recent_from],
            "rows": rows,
        }

    def radar(self):
        meets = {}
        for m in self.n.board_meetings():
            s, d = m.get("symbol"), parse_date(m.get("date"))
            if s not in self.stocks or not d:
                continue
            text = (m["purpose"] + " " + m["desc"]).lower()
            if "financial result" not in text and "results" not in text:
                continue
            d = d.date()
            if not (self.today <= d <= self.today + dt.timedelta(days=7)):
                continue
            k = (s, d)
            meets.setdefault(k, {"symbol": s, "date": d, "sources": set()})["sources"].add(m["source"])
        tomorrow = self.next_trading_day(self.today)
        out = {"A": [], "B1": [], "B2": [], "C": [], "D": [], "excluded": [], "pruned": [], "signals": []}
        for (s, d), m in sorted(meets.items(), key=lambda kv: kv[0][1]):
            x = self.stocks[s]
            expected_qe = last_quarter_end(d)
            if x["filings"] and x["filings"][0]["qe"] >= expected_qe.isoformat():
                out["pruned"].append({"name": x["name"], "date": d.isoformat(), "reason": "already reported"})
                continue
            a = self.assess(x, self.now, x["bars"], x["filings"][:5])
            if not a:
                out["excluded"].append({"name": x["name"], "date": d.isoformat(), "reason": "no filed quarterly numbers found"})
                continue
            item = {"name": x["name"], "sym": s, "date": d.isoformat(), "day": d.strftime("%a %d %b"),
                    "bullets": A.bullets(a["f"], a["pts"], a["t"], a["ev90"]), "tag": a["bucket"],
                    "verified": " + ".join(sorted(m["sources"])), "mcap_cr": a["mcap_cr"],
                    "signal": a["signal"], "points": a["pts"]}
            if d == tomorrow:
                out["C"].append(item)
            mc = a["mcap_cr"] or 0
            if a["bucket"] in ("A", "B") and mc < 5000:
                out["excluded"].append({"name": x["name"], "date": d.isoformat(), "reason": "market cap below Rs.5,000 cr"})
            elif a["bucket"] == "A":
                out["A"].append(item)
            elif a["bucket"] == "B":
                out["B1" if mc >= 20000 else "B2"].append(item)
            elif a["bucket"] == "D":
                out["D"].append(item)
            else:
                out["excluded"].append({"name": x["name"], "date": d.isoformat(), "reason": a["excluded"]})
            if a["signal"]:
                lv = A.levels(a["signal"], a["t"])
                if lv:
                    out["signals"].append({"signal": a["signal"], "name": x["name"], "sym": s, **lv,
                                           "trigger": f"Results on {d:%d %b}",
                                           "event_date": f"{d:%a %d %b %Y}", "event_kind": "Results due",
                                           "why": item["bullets"][0] if item["bullets"] else ""})
        out["tomorrow"] = tomorrow.isoformat()
        return out

    def event_signals(self):
        since = self.news_since()
        cands = []
        for s, x in self.stocks.items():
            for a in x["anns"]:
                when = parse_date(a.get("an_dt"))
                if not when or when < since or A.is_routine(a.get("desc")):
                    continue
                if s in getattr(self, "results_syms", ()):
                    continue
                cands.append((s, when, a))
        self.progress(f"Reading {len(cands)} recent non-routine filings")
        items, enriched = [], []
        for i, (s, when, a) in enumerate(sorted(cands, key=lambda c: c[1], reverse=True)[:150]):
            pdf = self.pdf_text(a.get("attchmntFile"))
            sent, strength, kw = A.classify(a.get("desc"), a.get("attchmntText"), pdf)
            if sent == "neutral":
                continue
            amt = A.amount_cr(pdf or a.get("attchmntText"))
            enriched.append({"id": str(i), "sym": s, "when": when, "a": a, "sent": sent,
                             "strength": strength, "kw": kw, "amt": amt, "pdf": pdf})
            items.append({"id": str(i), "text": f"{a.get('desc')}. {a.get('attchmntText')}. {pdf[:1500]}"})
        judged = llm.one_liners(items)
        lines = {k: v["line"] for k, v in judged.items() if v.get("line")}
        kept, dropped = [], []
        for e in enriched:
            x = self.stocks[e["sym"]]
            t = A.tech(x["bars"])
            f = A.quarter_facts(x["filings"][:5]) or {}
            e["mcap"] = f.get("shares") * t["close"] / 1e7 if (f.get("shares") and t) else None
            j = judged.get(e["id"], {})
            if e["amt"] and e["mcap"] and e["amt"] < 0.001 * e["mcap"]:
                dropped.append(f"{x['name']}: Rs.{A.inr(e['amt'], 2)} cr is under 0.1% of market cap")
                continue
            if j.get("material") is False or j.get("impact") == "neutral":
                dropped.append(f"{x['name']}: {j.get('line') or e['a'].get('desc')} (LLM: not price-moving)")
                continue
            llm_sent = {"positive": "pos", "negative": "neg"}.get(j.get("impact"))
            if llm_sent and llm_sent != e["sent"]:
                e["sent"], e["strength"] = llm_sent, 1
            kept.append(e)
        self.filtered_events = dropped
        latest = {}
        for e in kept:
            if e["sym"] not in latest or e["when"] > latest[e["sym"]]["when"]:
                latest[e["sym"]] = e
        out = []
        for s, e in latest.items():
            x = self.stocks[s]
            t = A.tech(x["bars"])
            mcap = e["mcap"]
            if e["sent"] == "pos":
                sig = "WATCH UP"
            else:
                sig = "STRONG SELL" if e["strength"] >= 3 else "SELL"
            ri = A.reaction_index(x["bars"], e["when"])
            since_ev = A.chain_ret(x["bars"], ri, len(x["bars"]) - ri) if ri is not None else None
            note = ""
            if since_ev is not None:
                agree = (since_ev > 0) == ("BUY" in sig)
                note = f"stock {A.pct(since_ev, 1)} since" + ("" if agree or abs(since_ev) < .02 else " (market disagrees)")
            lv = A.levels(sig, t)
            if not lv:
                continue
            fallback = f"{e['a'].get('desc')}: '{e['kw']}'" + (f", about Rs.{A.inr(e['amt'])} cr" if e["amt"] else "")
            out.append({"signal": sig, "name": x["name"], "sym": s, **lv,
                        "trigger": f"{e['a'].get('desc')} ({e['when']:%d %b %H:%M})",
                        "event_date": f"{e['when']:%a %d %b %Y, %H:%M}", "event_kind": e["a"].get("desc") or "Filing",
                        "why": lines.get(e["id"]) or fallback, "reaction": note,
                        "source": e["a"].get("attchmntFile")})
        return out

    def case_studies(self):
        out = []
        start = self.today - dt.timedelta(days=400)
        for s in CASE_SYMBOLS:
            try:
                fil = self.quarters_with_numbers(self.n.filings(s), 9)
                bars = self.n.history(s, start)
                anns = self.n.announcements(self.today - dt.timedelta(days=365), self.today, symbol=s, ttl=86400)
            except RuntimeError as e:
                self.notes.append(f"case {s}: {e}")
                continue
            name = self.stocks.get(s, {}).get("name") or (fil[0]["name"] if fil else s)
            results = []
            for i, q in enumerate(fil[:5]):
                f = A.quarter_facts(fil[i:i + 5])
                when = dt.datetime.fromisoformat(q["when"])
                ri = A.reaction_index(bars, when)
                if ri is None or ri == 0:
                    continue
                results.append({"quarter": A.quarter_label(q["qe"]), "filed": when.strftime("%d %b %Y %H:%M"),
                                "rev_yoy": f.get("rev_yoy"), "pat_yoy": f.get("pat_yoy"),
                                "pre_r21": A.tech(bars[:ri])["r21"] if A.tech(bars[:ri]) else None,
                                "r1": A.chain_ret(bars, ri, 1), "r5": A.chain_ret(bars, ri, 5)})
            big = []
            one_year = (self.today - dt.timedelta(days=365)).isoformat()
            for i, b in enumerate(bars):
                if b["d"] < one_year or i < 30:
                    continue
                r = b["c"] / b["pc"] - 1
                avgv = sum(x["v"] for x in bars[i - 30:i]) / 30
                vx = b["v"] / avgv if avgv else 0
                if abs(r) >= .06 or (abs(r) >= .035 and vx >= 3):
                    d = dt.date.fromisoformat(b["d"])
                    prev_d = dt.date.fromisoformat(bars[i - 2]["d"])
                    lo = dt.datetime.combine(prev_d, dt.time(15, 30))
                    hi = dt.datetime.combine(d, dt.time(15, 30))
                    trig = []
                    for q in fil:
                        w = dt.datetime.fromisoformat(q["when"])
                        if lo <= w <= hi:
                            trig.append(f"{A.quarter_label(q['qe'])} results filed {w:%d %b %H:%M}")
                    for a in anns:
                        w = parse_date(a.get("an_dt"))
                        if w and lo <= w <= hi and not A.is_routine(a.get("desc")):
                            pdf = self.pdf_text(a.get("attchmntFile"))
                            sent, _, kw = A.classify(a.get("desc"), a.get("attchmntText"), pdf)
                            snippet = (pdf[:220] + "...") if pdf else a.get("attchmntText", "")[:160]
                            trig.append(f"{a.get('desc')} (filed {w:%d %b %H:%M}): {snippet}")
                    big.append({"date": b["d"], "ret": r, "vol_x": vx,
                                "trigger": trig[:2] or ["No exchange filing that day (market or sector move)"]})
            idx, peak, peak_d, dd, dd_from, dd_to = 1.0, 1.0, None, 0, None, None
            path = []
            for b in bars:
                if b["d"] < one_year:
                    continue
                idx *= b["c"] / b["pc"]
                path.append((b["d"], idx))
                if idx > peak:
                    peak, peak_d = idx, b["d"]
                if idx / peak - 1 < dd:
                    dd, dd_from, dd_to = idx / peak - 1, peak_d, b["d"]
            rec = None
            if dd_to:
                trough = next(v for d, v in path if d == dd_to)
                rec = path[-1][1] / trough - 1
            yr = path[-1][1] - 1 if path else None
            facts = [f"{name} 12-month return {A.pct(yr)}; deepest fall {A.pct(dd)} ({dd_from} to {dd_to}), "
                     f"recovered {A.pct(rec)} since."]
            for r in results:
                facts.append(f"{r['quarter']} results ({r['filed']}): revenue {A.pct(r['rev_yoy'])} YoY, profit "
                             f"{A.pct(r['pat_yoy'])} YoY; stock {A.pct(r['pre_r21'])} in month before, "
                             f"{A.pct(r['r1'], 1)} next day, {A.pct(r['r5'], 1)} over 5 days.")
            for m in sorted(big, key=lambda m: -abs(m["ret"]))[:6]:
                facts.append(f"{m['date']}: {A.pct(m['ret'], 1)} on {m['vol_x']:.1f}x volume; trigger: {' | '.join(m['trigger'])}")
            facts_text = "\n".join(facts[1:])
            out.append({"sym": s, "name": name, "year_ret": yr, "max_dd": dd, "dd_from": dd_from, "dd_to": dd_to,
                        "recovery": rec, "results": results,
                        "big_moves": sorted(big, key=lambda m: m["date"], reverse=True)[:10],
                        "story": llm.narrative(facts_text) or default_story(results, big)})
        return out


def default_story(results, big):
    parts = []
    good = [r for r in results if (r["pat_yoy"] or 0) > .2 and (r["r1"] or 0) > 0]
    if good:
        avg = sum(r["r1"] for r in good) / len(good)
        parts.append(f"{len(good)} of {len(results)} recent results showed profit growth above 20% YoY; "
                     f"the stock rose {A.pct(avg, 1)} on average the next day.")
    ups = [m for m in big if m["ret"] > 0]
    downs = [m for m in big if m["ret"] < 0]
    if ups:
        parts.append(f"{len(ups)} big up-days in the year, mostly around: {ups[0]['trigger'][0][:90]}.")
    if downs:
        parts.append(f"{len(downs)} big down-days; latest trigger: {downs[0]['trigger'][0][:90]}.")
    return " ".join(parts) or "No outsized moves in the last year."


def last_quarter_end(d: dt.date):
    for m in (12, 9, 6, 3):
        y = d.year
        cand = dt.date(y, m, 31 if m in (12, 3) else 30)
        if cand < d:
            return cand
    return dt.date(d.year - 1, 12, 31)


def diff(prev, cur):
    if not prev:
        return {"first_run": True}

    def names(r):
        return {k: {i["name"] for i in r.get(k, [])} for k in ("A", "B1", "B2", "C", "D")}

    p, c = names(prev), names(cur)
    out = {"added": {}, "removed": {}}
    for k in c:
        add, rem = c[k] - p.get(k, set()), p.get(k, set()) - c[k]
        if add:
            out["added"][k] = sorted(add)
        if rem:
            out["removed"][k] = sorted(rem)
    ps = {(s["name"], s["signal"]) for s in prev.get("signals", [])}
    cs = {(s["name"], s["signal"]) for s in cur.get("signals", [])}
    out["new_signals"] = sorted(f"{n}: {s}" for n, s in cs - ps)
    out["dropped_signals"] = sorted(f"{n}: {s}" for n, s in ps - cs)
    return out


SIGNAL_EMOJI = {"STRONG BUY": "🟢🚀", "BUY": "🟢", "BUY (RISKY)": "🟡", "SELL": "🔴", "STRONG SELL": "🔴🔻",
                "WATCH UP": "👀⬆️", "WATCH DOWN": "👀⬇️"}
TAG_EMOJI = {"A": "⭐", "B-L1": "✅", "B-L2": "✅", "D": "⚠️"}


def telegram_text(r):
    url = os.getenv("DASHBOARD_URL", "http://localhost:8379")
    L = [f"📡 <b>Pre-Results Radar</b> | {r['generated_at']}"]
    week = {}
    for tag, key in (("A", "A"), ("B-L1", "B1"), ("B-L2", "B2"), ("D", "D")):
        for i in r.get(key, []):
            week.setdefault(i["date"], []).append((i["name"], tag, i["bullets"][0] if i["bullets"] else ""))
    L.append("\n📅 <b>Week ahead (F&amp;O results)</b>")
    if not week:
        L.append("No qualifying F&amp;O names reporting in the next 7 days.")
    for d in sorted(week):
        L.append(f"<u>{dt.date.fromisoformat(d):%a %d %b}</u>")
        for n_, tag, why in week[d]:
            L.append(f"{TAG_EMOJI.get(tag, '•')} {esc(n_)} [{tag}] {esc(why)}")
    if r.get("C"):
        L.append("\n⏰ <b>Tomorrow</b>: " + ", ".join(esc(i["name"]) for i in r["C"]))
    if r.get("signals"):
        L.append("\n📊 <b>Signals</b> (results movers first; CMP = live price or last close)")
        prio = {"results": 0, "follow-through": 1, "pre-results": 2, "news": 3}
        for s in sorted(r["signals"], key=lambda x: prio.get(x.get("kind"), 9))[:20]:
            head = f"\n{SIGNAL_EMOJI.get(s['signal'], '•')} <b>{s['signal']}</b> · <b>{esc(s['name'])}</b>\n"
            when = f"   📅 {esc(s['event_date'])} · {esc(s.get('event_kind', ''))}\n" if s.get("event_date") else ""
            cond = "if it confirms: " if s["signal"].startswith("WATCH") else ""
            price = f"   CMP {A.inr(s['cmp'], 2)} | {cond}🎯 {A.inr(s['target'], 2)} | 🛑 {A.inr(s['stop'], 2)}\n"
            L.append(head + when + price + f"   {esc(s['why'][:230])}")
    if r.get("themes"):
        L.append("\n🔥 <b>Live sector themes</b> (3+ F&amp;O names in NSE top-20 movers)")
        for t in r["themes"][:4]:
            L.append(f"{'⬆️' if t['side'] == 'up' else '⬇️'} <b>{esc(t['industry'])}</b>: "
                     + ", ".join(f"{esc(m['name'].split(' Ltd')[0])} {A.pct(m['chg'], 1)}" for m in t["movers"][:6]))
    if r.get("likely"):
        x0 = r["likely"][0]
        L.append(f"\n🎲 <b>Likely big movers on {dt.date.fromisoformat(x0['for_session']):%a %d %b}</b> "
                 f"(prediction from the {dt.date.fromisoformat(x0['as_of']):%d %b} close; chance of a top-10 F&amp;O "
                 f"gainer or loser, normal is 9%; direction ↕️ not predictable)")
        for x in r["likely"][:8]:
            arrow = {"UP": "⬆️", "DOWN": "⬇️"}.get(x["lean"], "↕️")
            L.append(f"{arrow} {esc(x['name'])}: {x['p'] * 100:.0f}% | {esc(x['why'][:90])}")
    ts = r.get("track_summary")
    if ts:
        L.append(f"\n🧾 <b>Tracker</b>: {ts['n']} closed signals, {ts['working'] * 100:.0f}% made money, "
                 f"average {A.pct(ts['avg'], 1)}; {ts['open']} still open")
    ch = r.get("changes", {})
    if ch and not ch.get("first_run"):
        bits = [f"+{k}: {', '.join(v)}" for k, v in ch.get("added", {}).items()]
        bits += [f"-{k}: {', '.join(v)}" for k, v in ch.get("removed", {}).items()]
        bits += [f"new: {x}" for x in ch.get("new_signals", [])[:8]]
        if bits:
            L.append("\n🔄 <b>Changes</b>\n" + esc("\n".join(bits)))
    if r.get("excluded"):
        L.append("\n🚫 <b>Excluded</b>: " + esc("; ".join(f"{e['name']} ({e['reason']})" for e in r["excluded"][:10])))
    bt = r.get("backtest", {})
    for row in bt.get("summary", []):
        if row["signal"] in ("STRONG BUY", "BUY") and row.get("win_r1") is not None:
            L.append(f"📈 Backtest {row['signal']}: win {row['win_r1'] * 100:.0f}% next day, avg {A.pct(row['avg_r1'], 1)} (n={row['n_r1']})")
    L.append(f"\n🔗 Dashboard: {url}")
    return "\n".join(L)


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main(send=False, progress=None):
    r = Job(progress).run()
    if send:
        try:
            st_ = telegram.send(telegram_text(r))
        except Exception as e:
            st_ = [f"error: {e.__class__.__name__}"]
        r["telegram"] = st_
        print("telegram:", st_, flush=True)
    return r


if __name__ == "__main__":
    main(send="--telegram" in sys.argv)
