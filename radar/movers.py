import json
import datetime as dt
from pathlib import Path

import analysis as A

TRACK = Path(__file__).resolve().parent.parent / "data" / "track.json"
CONFIRM = 0.03


def lean_of(fil):
    """Direction lean from the reported quarter: acceleration (x2), profit YoY, revenue YoY."""
    f = A.quarter_facts(fil[:5])
    fp = A.quarter_facts(fil[1:6]) if len(fil) >= 6 else None
    if not f:
        return None, 0, None
    accel = None
    if fp and f.get("pat_yoy") is not None and fp.get("pat_yoy") is not None:
        accel = f["pat_yoy"] - fp["pat_yoy"]
    sc = 0
    if accel is not None:
        sc += 2 if accel > 0 else -2
    for k in ("pat_yoy", "rev_yoy"):
        if f.get(k) is not None:
            sc += 1 if f[k] > 0 else -1
    if f.get("loss"):
        sc -= 2
    return f, sc, accel


def build(job):
    """Returns (board rows, signals, set of symbols whose move is results-driven)."""
    board, signals, results_syms = [], [], set()
    session_date = job.today.isoformat()
    for s, x in job.stocks.items():
        fil, bars = x["filings"], x["bars"]
        if not fil or len(bars) < 30:
            continue
        when = dt.datetime.fromisoformat(fil[0]["when"])
        age_days = (job.now - when).days
        if age_days > 8:
            continue
        ri = A.reaction_index(bars, when)
        f, sc, accel = lean_of(fil)
        if not f:
            continue
        results_syms.add(s)
        t = A.tech(bars)
        lean = "UP" if sc >= 2 else "DOWN" if sc <= -2 else "MIXED"
        facts = (f"{A.quarter_label(f['qe'])}: profit {A.pct(f.get('pat_yoy'))} YoY, revenue {A.pct(f.get('rev_yoy'))} YoY"
                 + (f", profit growth {'accelerating' if accel > 0 else 'slowing'} vs previous quarter" if accel is not None else ""))
        row = {"sym": s, "name": x["name"], "filed": when.strftime("%a %d %b %H:%M"), "lean": lean, "facts": facts,
               "pat_yoy": f.get("pat_yoy"), "rev_yoy": f.get("rev_yoy")}
        if ri is None:
            q = None
            try:
                q = job.n.live_quote(s)
            except RuntimeError:
                pass
            live = q and q.get("open") and q.get("time", "").startswith(job.now.strftime("%d-%b-%Y"))
            row.update(stage="fresh", live_chg=q["chg"] if live else None,
                       gap=(q["open"] / q["prev"] - 1) if live and q.get("prev") else None)
            sig, why = None, None
            if live:
                c = q["chg"]
                if c >= CONFIRM:
                    sig = "STRONG BUY" if c >= .05 and lean != "DOWN" else "BUY"
                    why = (f"Results reaction confirmed up ({A.pct(c, 1)} today). Past results days opening +3% or more "
                           f"finished as a top-10 F&O gainer 65% of the time.")
                elif c <= -CONFIRM:
                    sig = "STRONG SELL" if c <= -.05 and lean != "UP" else "SELL"
                    why = (f"Results reaction confirmed down ({A.pct(c, 1)} today). Past results days opening -3% or worse "
                           f"finished as a top-10 F&O loser 77% of the time.")
                row["status"] = "confirmed " + ("up" if c >= CONFIRM else "down") if sig else f"no confirmation yet ({A.pct(c, 1)})"
            else:
                row["status"] = "reacts next session"
                sig = {"UP": "WATCH UP", "DOWN": "WATCH DOWN"}.get(lean)
                why = (f"Fresh results, market has not reacted yet. Lean {lean.lower()} from the numbers "
                       f"(right about 55-60% of the time); confirm with the opening move of 3% or more.")
            if sig:
                price = q["last"] if live else (t["close"] if t else None)
                tt = dict(t, close=price) if t and price else t
                lv = A.levels(sig.replace("WATCH UP", "BUY").replace("WATCH DOWN", "SELL"), tt)
                if lv:
                    signals.append({"signal": sig, "name": x["name"], "sym": s, **lv, "kind": "results",
                                    "trigger": f"Results filed {when:%d %b %H:%M}", "event_date": f"{when:%a %d %b %Y, %H:%M}",
                                    "event_kind": "Results", "why": f"{facts}. {why}",
                                    "live": bool(live)})
        else:
            r1 = A.chain_ret(bars, ri, 1)
            since = A.chain_ret(bars, ri, len(bars) - ri)
            sessions = len(bars) - ri
            row.update(stage="reacted", r1=r1, since=since, sessions=sessions,
                       status=f"day 1 {A.pct(r1, 1)}, {A.pct(since, 1)} over {sessions} session(s)")
            if r1 is not None and r1 >= .05 and sessions <= 3 and t:
                lv = A.levels("BUY", t)
                if lv:
                    signals.append({"signal": "BUY", "name": x["name"], "sym": s, **lv, "kind": "follow-through",
                                    "trigger": f"Results day {A.pct(r1, 1)} on {bars[ri]['d']}",
                                    "event_date": f"{when:%a %d %b %Y, %H:%M}", "event_kind": "Results follow-through",
                                    "why": f"{facts}. Results days of +5% or more gained a further +1.1% on average over the "
                                           f"next 4 sessions (up 61% of the time).", "live": False})
        board.append(row)
    board.sort(key=lambda r: (r["stage"] != "fresh", -abs(r.get("live_chg") or r.get("r1") or 0)))
    return board, signals, results_syms


HORIZON = {"news": 2, "news (old rules)": 2, "results": 5, "follow-through": 4, "pre-results": 5}


def _is_up(sig):
    return "BUY" in sig or sig == "WATCH UP"


def track(job, signals):
    """Log every signal, then follow it bar by bar: target hit, stopped out, or closed at its holding period.
    Stops are checked before targets on the same day (conservative)."""
    try:
        log = json.loads(TRACK.read_text())
    except Exception:
        log = []
    today = job.today.isoformat()
    have = {(r["sym"], r["signal"], r["event_date"]) for r in log}
    for s in signals:
        key = (s["sym"], s["signal"], s.get("event_date") or today)
        if key in have or not s.get("entry") or s["signal"].startswith("WATCH"):
            continue
        log.append({"sym": s["sym"], "name": s["name"], "signal": s["signal"], "event_date": key[2],
                    "kind": s.get("kind", "news"), "first_seen": job.now.strftime("%Y-%m-%d %H:%M"),
                    "entry": s["entry"], "target": s.get("target"), "stop": s.get("stop")})
    cutoff = (job.today - dt.timedelta(days=21)).isoformat()
    log = [r for r in log if r["first_seen"][:10] >= cutoff]
    for r in log:
        if r.get("closed"):
            continue
        bars = list(job.stocks.get(r["sym"], {}).get("bars") or [])
        q = None
        try:
            q = job.n.live_quote(r["sym"], ttl=120)
        except RuntimeError:
            pass
        if q and q.get("last") and q.get("high") and (q.get("time") or "").startswith(job.now.strftime("%d-%b-%Y")) \
                and (not bars or bars[-1]["d"] < today):
            bars.append({"d": today, "o": q.get("open"), "h": q["high"], "l": q["low"], "c": q["last"], "live": True})
        seen = dt.datetime.strptime(r["first_seen"], "%Y-%m-%d %H:%M")
        after = [b for b in bars if b["d"] > seen.date().isoformat()
                 or (b["d"] == seen.date().isoformat() and seen.time() < dt.time(15, 30))]
        up = _is_up(r["signal"])
        horizon = HORIZON.get(r["kind"], 3)
        exit_px, status, used = None, None, len(after)
        for k, b in enumerate(after):
            o = b.get("o")
            first_day = k == 0 and b["d"] == seen.date().isoformat() and seen.time() >= dt.time(9, 15)
            hit_stop = r.get("stop") and (b["l"] <= r["stop"] if up else b["h"] >= r["stop"])
            hit_tgt = r.get("target") and (b["h"] >= r["target"] if up else b["l"] <= r["target"])
            if hit_stop:
                gap_through = o and not first_day and (o <= r["stop"] if up else o >= r["stop"])
                exit_px, status = (o, "stopped (gapped through, exit at open)") if gap_through else (r["stop"], "stopped")
            elif hit_tgt:
                gap_through = o and not first_day and (o >= r["target"] if up else o <= r["target"])
                exit_px, status = (o, "target hit (gapped through, exit at open)") if gap_through else (r["target"], "target hit")
            elif k + 1 >= horizon and not b.get("live"):
                exit_px, status = b["c"], "closed at horizon"
            if status:
                r.update(closed=b["d"], exit=exit_px)
                used = k + 1
                break
        last = exit_px or (after[-1]["c"] if after else (q or {}).get("last") or (bars[-1]["c"] if bars else None))
        if not last:
            continue
        r["last"] = last
        r["ret"] = last / r["entry"] - 1
        r["with_signal"] = r["ret"] if up else -r["ret"]
        r["sessions"] = used
        r["horizon"] = horizon
        r["status"] = status or ("open, working" if r["with_signal"] > 0.002 else "open, against" if r["with_signal"] < -0.002 else "open, flat")
    TRACK.parent.mkdir(parents=True, exist_ok=True)
    TRACK.write_text(json.dumps(log, indent=1))
    closed = [r for r in log if r.get("closed") and r.get("with_signal") is not None]
    summary = None
    if closed:
        summary = {"n": len(closed), "working": sum(r["with_signal"] > 0 for r in closed) / len(closed),
                   "avg": sum(r["with_signal"] for r in closed) / len(closed),
                   "open": sum(1 for r in log if not r.get("closed"))}
    return sorted(log, key=lambda r: r["first_seen"], reverse=True), summary


def likely(job):
    """Next-session 'likely big movers': stocks whose features as of the last close match buckets that the
    day-before study (radar/daystudy.py) found 2x+ more likely to finish among the top-10 gainers or losers.
    Direction is only given where the evidence has one (results lean, negative news)."""
    path = TRACK.parent / "daystudy.json"
    if not path.exists():
        return []
    st = json.loads(path.read_text())
    B, base = st["buckets"], st["base"]["top10"] + st["base"]["bot10"]
    prob = {k: v["top10"] + v["bot10"] for k, v in B.items()}
    last_d = max((x["bars"][-1]["d"] for x in job.stocks.values() if x["bars"]), default=None)
    if not last_d:
        return []
    rets = {s: x["bars"][-1]["c"] / x["bars"][-1]["pc"] - 1 for s, x in job.stocks.items()
            if x["bars"] and x["bars"][-1]["d"] == last_d}
    med = sorted(rets.values())[len(rets) // 2] if rets else 0
    oi = job.n.fo_oi(last_d) or {}
    close_dt = dt.datetime.combine(dt.date.fromisoformat(last_d), dt.time(15, 30))
    out = []
    for s, x in job.stocks.items():
        bars = x["bars"]
        if len(bars) < 25 or bars[-1]["d"] != last_d:
            continue
        b, hist = bars[-1], bars[-21:-1]
        avgv = sum(h["v"] or 0 for h in hist) / 20
        r, volx = rets[s] - med, ((b["v"] or 0) / avgv if avgv else 0)
        reasons, lean = [], None
        fil = x["filings"]
        if fil and dt.datetime.fromisoformat(fil[0]["when"]) > close_dt:
            f, sc, _ = lean_of(fil)
            reasons.append(("Overnight results filing", f"results filed {dt.datetime.fromisoformat(fil[0]['when']):%a %H:%M}"))
            lean = "UP" if sc >= 2 else "DOWN" if sc <= -2 else None
        neg = [e for e in x["events"] if e["when"] > close_dt and e["sent"] == "neg"]
        if neg and not lean:
            reasons.append(("Overnight news: negative (rules)", neg[0]["line"]))
            lean = "DOWN"
        if r >= .05:
            reasons.append(("Today >= +5% vs market", f"{A.pct(r, 1)} vs market"))
        if r <= -.05:
            reasons.append(("Today <= -5% vs market", f"{A.pct(r, 1)} vs market"))
        if volx >= 3:
            reasons.append(("Volume >= 3x, closed up" if r > 0 else "Volume >= 3x, closed down", f"volume {volx:.1f}x"))
        if s in oi and oi[s][0] - oi[s][1] > 0:
            doi = oi[s][1] / (oi[s][0] - oi[s][1])
            if doi > .15:
                reasons.append(("OI: big OI jump (+15%) any direction", f"futures OI {A.pct(doi)}"))
            elif doi > .05 and r < -.02:
                reasons.append(("OI: short build-up (down >2%, OI +5%)", f"short build-up, OI {A.pct(doi)}"))
        scored = [(prob[k], k, why) for k, why in reasons if k in prob]
        if not scored:
            continue
        p = max(scored)[0]
        if p < 2 * base:
            continue
        out.append({"sym": s, "name": x["name"], "p": p, "lift": p / base, "lean": lean or "EITHER",
                    "why": "; ".join(w for _, _, w in sorted(scored, reverse=True)), "as_of": last_d})
    nxt = job.next_trading_day(dt.date.fromisoformat(last_d))
    for r in out:
        r["for_session"] = nxt.isoformat()
    out.sort(key=lambda r: -r["p"])
    return out[:20]


def themes(job, ind_of):
    """During market hours: industries with 3+ F&O names in NSE's live top-20 gainers or losers."""
    try:
        mv = job.n.fo_movers()
    except RuntimeError:
        return []
    if not mv.get("time") or not mv["time"].startswith(job.now.strftime("%d-%b-%Y")):
        return []
    out = []
    for side in ("gainers", "losers"):
        grp = {}
        for r in mv[side]:
            grp.setdefault(ind_of.get(r["sym"], "Other"), []).append(r)
        for ind, rs in grp.items():
            if len(rs) >= 3 and ind != "Other":
                members = [s for s, i in ind_of.items() if i == ind and s in job.stocks]
                listed = {r["sym"] for r in rs}
                out.append({"side": "up" if side == "gainers" else "down", "industry": ind, "time": mv["time"],
                            "movers": [{"sym": r["sym"], "name": job.stocks.get(r["sym"], {}).get("name", r["sym"]),
                                        "chg": r["chg"]} for r in rs],
                            "others": [job.stocks[s]["name"] for s in members if s not in listed][:12]})
    out.sort(key=lambda t: -len(t["movers"]))
    return out
