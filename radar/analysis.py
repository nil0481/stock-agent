"""Scoring, event classification, signals and backtest. Pure functions over NSE data."""
import re
import statistics as st
import datetime as dt

# ---------------------------------------------------------------- formatting
def inr(x, dec=0):
    """Indian digit grouping: 140800 -> '1,40,800'."""
    if x is None:
        return "n/a"
    neg = x < 0
    x = abs(x)
    s = f"{x:.{dec}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + ("." + frac if frac else "")


def pct(x, dec=0):
    return "n/a" if x is None else f"{x * 100:+.{dec}f}%"


def growth(a, b):
    if a is None or b is None or b == 0:
        return None
    if b < 0:  # turning from loss: growth % not meaningful
        return None
    return a / b - 1


# ---------------------------------------------------------------- announcements
ROUTINE = (
    "newspaper", "trading window", "analysts/institutional investor meet", "investor meet",
    "schedule of meet", "link of recording", "transcript", "loss of share", "duplicate share",
    "certificate under", "74(5)", "compliance certificate", "monitoring agency", "statement of deviation",
    "esop", "esos", "employee stock", "registrar", "rta", "kyc", "secretarial compliance",
    "book closure", "record date", "investor presentation", "shareholders meeting", "agm", "postal ballot",
    "change in director", "appointment of", "re-appointment", "clarification - financial results",
    "copy of", "outcome of board meeting", "financial result", "press release", "appointment",
    "change in management", "cessation", "change in directorate", "change in key managerial",
    "allotment of securities", "allotment of", "takeover regulations", "sast", "pledge", "encumbrance",
    "issue of securities", "debt securities", "commercial paper", "non-convertible", "record date",
)
RELIEF = ("approved the resolution plan", "approval of resolution plan", "stayed", "stay on", "stay order", "set aside", "quashed", "revoked the", "withdrawn the direction",
          "favourable order", "in favour of the company", "allowed the appeal")
STRONG_NEG = (
    "closure of manufacturing", "closure direction", "directing closure", "stop production",
    "sebi order", "search and seizure", "search operation", "income tax search", "forensic audit",
    "fraud", "auditor has resigned", "resignation of statutory auditor", "resignation of auditor",
    "default in payment", "default in repayment", "insolvency", "admitted by nclt", "warning letter",
    "import alert", "official action indicated", "enforcement directorate", "suspension of operations",
    "fire at", "fire broke", "explosion", "plant shutdown", "cancellation of licence", "licence cancelled",
)
NEG = (
    "penalty of", "penalty imposed", "imposed a penalty", "demand order", "tax demand", "gst demand", "show cause", "show-cause", "downgrade",
    "outlook revised to negative", "workers strike", "labour strike", "went on strike", "lockout", "termination of contract", "cancellation of order",
    "litigation", "adverse", "resignation of chief executive", "resignation of chief financial",
    "resignation of managing director", "resignation of ceo", "resignation of cfo", "resigned as ceo",
    "resigned as cfo", "action(s) taken or orders passed",
)
STRONG_POS = (
    "letter of award", "letter of acceptance", "highest ever", "record order", "buyback", "buy back",
    "bonus issue", "rating upgrade", "upgraded", "establishment inspection report", "eir", "no action indicated",
    "voluntary action indicated", "final approval", "acquisition of",
)
POS = (
    "bagging", "receiving of orders", "order worth", "order for", "orders worth", "contract", "award of",
    "commissioning", "capacity addition", "commercial production", "launch", "partnership", "mou",
    "usfda approval", "approval from usfda", "regulatory approval", "received approval", "expansion",
    "new plant", "joint venture",
)
GOVERNANCE = ("sebi order", "forensic audit", "fraud", "auditor has resigned", "resignation of statutory auditor",
              "resignation of auditor", "default in payment", "default in repayment", "enforcement directorate",
              "insolvency")
AMOUNT = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(crore|crores|cr|lakh|lakhs|million|mn|billion|bn)\b", re.I)


def amount_cr(text):
    best = None
    for num, unit in AMOUNT.findall(text or ""):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        u = unit.lower()
        v = v if u.startswith("cr") else v / 100 if u.startswith("lakh") else v / 10 if u in ("million", "mn") else v * 100
        best = max(best or 0, v)
    return best


_PAT = {}


def _has(words, text):
    for k in words:
        p = _PAT.get(k)
        if p is None:
            p = _PAT[k] = re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])")
        for m in p.finditer(text):
            before = text[max(0, m.start() - 80):m.start()]
            if re.search(r"\b(not|no|neither|nor|without|never)\b", before):
                continue  # negated boilerplate, e.g. "not debarred ... by any SEBI order"
            return k
    return None


def classify(desc, text, pdf_text=""):
    """Return (sentiment, strength 1-3, matched keyword). Deterministic keyword rules."""
    d = (desc or "").lower()
    full = " ".join([d, (text or "").lower(), (pdf_text or "").lower()[:4000]])
    if any(k in d for k in ROUTINE) and not pdf_text:
        return "neutral", 0, None
    k = _has(RELIEF, full)
    if k:
        return "pos", 2, k
    k = _has(STRONG_NEG, full)
    if k:
        return "neg", 3, k
    k = _has(NEG, full)
    if k:
        return "neg", 2, k
    k = _has(STRONG_POS, full)
    if k:
        return "pos", 2 if k == "acquisition of" else 3, k
    k = _has(POS, full)
    if k:
        return "pos", 1, k
    return "neutral", 0, None


def is_routine(desc):
    d = (desc or "").lower()
    return any(k in d for k in ROUTINE)


# ---------------------------------------------------------------- price helpers
def idx_on_or_after(bars, date_iso):
    for i, b in enumerate(bars):
        if b["d"] >= date_iso:
            return i
    return None


def reaction_index(bars, when: dt.datetime):
    """First bar whose close reflects an event published at `when` (IST)."""
    t = when.time()
    if t <= dt.time(15, 30):
        return idx_on_or_after(bars, when.date().isoformat())
    nxt = (when.date() + dt.timedelta(days=1)).isoformat()
    return idx_on_or_after(bars, nxt)


def chain_ret(bars, i0, n):
    """Return over n bars starting at bar i0 (uses NSE prev-close, so corporate actions are adjusted)."""
    if i0 is None or i0 + n > len(bars):
        return None
    r = 1.0
    for b in bars[i0:i0 + n]:
        r *= b["c"] / b["pc"]
    return r - 1


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for b in bars[-n:]:
        trs.append(max(b["h"] - b["l"], abs(b["h"] - b["pc"]), abs(b["l"] - b["pc"])))
    return sum(trs) / n


def tech(bars_before):
    """Momentum/room facts from bars strictly before the event."""
    if len(bars_before) < 22:
        return None
    closes = [b["c"] for b in bars_before]
    c = closes[-1]
    r21 = 1.0
    for b in bars_before[-21:]:
        r21 *= b["c"] / b["pc"]
    r21 -= 1
    vols = [b["v"] or 0 for b in bars_before]
    v5 = sum(vols[-5:]) / 5
    v30 = sum(vols[-35:-5]) / max(1, len(vols[-35:-5]))
    return {"close": c, "sma20": sma(closes, 20), "sma50": sma(closes, 50), "r21": r21,
            "vol_ratio": (v5 / v30) if v30 else None, "atr": atr(bars_before)}


# ---------------------------------------------------------------- scoring
def quarter_facts(qs):
    """qs: quarters newest first, each with rev_cr/pat_cr. L = qs[0]."""
    L = qs[0] if qs else None
    if not L:
        return None
    f = {"qe": L["qe"], "rev": L.get("rev_cr"), "pat": L.get("pat_cr")}
    f["rev_qoq"] = growth(L.get("rev_cr"), qs[1].get("rev_cr")) if len(qs) > 1 else None
    yoy = qs[4] if len(qs) > 4 else None
    f["rev_yoy"] = growth(L.get("rev_cr"), yoy.get("rev_cr")) if yoy else None
    f["pat_yoy"] = growth(L.get("pat_cr"), yoy.get("pat_cr")) if yoy else None
    f["pat_prev_year"] = yoy.get("pat_cr") if yoy else None
    ups = 0
    trans = 0
    for a, b in zip(qs[0:3], qs[1:4]):
        if a.get("rev_cr") is not None and b.get("rev_cr") is not None:
            trans += 1
            ups += a["rev_cr"] > b["rev_cr"]
    f["rev_up_count"], f["rev_trans"] = ups, trans
    pats = [q.get("pat_cr") for q in qs[:4] if q.get("pat_cr") is not None]
    f["all_profitable"] = len(pats) == 4 and all(p > 0 for p in pats)
    f["loss"] = (L.get("pat_cr") or 0) < 0
    shares = None
    if L.get("paidup") and L.get("fv"):
        shares = L["paidup"] / L["fv"]
    f["shares"] = shares
    return f


def score(f, t, events90, sector_rel):
    """Return dict of component points. Max raw = 85 (analyst estimates unavailable -> rescaled)."""
    pts = {}
    # growth 25
    g = 0
    py, ry = f.get("pat_yoy"), f.get("rev_yoy")
    if f.get("pat") is not None and f.get("pat_prev_year") is not None and f["pat_prev_year"] <= 0 < f["pat"]:
        g += 12  # turned profitable
    elif py is not None:
        g += 15 if py >= .30 else 11 if py >= .15 else 7 if py >= .05 else 4 if py >= 0 else 0
    if ry is not None:
        g += 10 if ry >= .20 else 7 if ry >= .10 else 4 if ry >= .05 else 2 if ry >= 0 else 0
    pts["growth"] = g
    # positives 15 (exchange announcements, last 90 days)
    p = 0
    for e in events90:
        if e["sent"] == "pos":
            p += 5 if e["strength"] >= 3 else 3
        elif e["sent"] == "neg":
            p -= 8 if e["strength"] >= 3 else 4
    pts["positives"] = max(0, min(15, p))
    # sector 10 (industry peers' 1-month price trend vs F&O median)
    if sector_rel is None:
        pts["sector"] = 5
    else:
        pts["sector"] = 10 if sector_rel > .03 else 7 if sector_rel > 0 else 4 if sector_rel > -.03 else 1
    # history 15
    h = 0
    if f.get("rev_trans"):
        h += round(9 * f["rev_up_count"] / f["rev_trans"])
    h += 3 if f.get("all_profitable") else 0
    h += 3 if (py is not None and py > 0) else 0
    pts["history"] = h
    # momentum 10 / room 10
    if t:
        m = 0
        m += 3 if t["sma20"] and t["close"] > t["sma20"] else 0
        m += 4 if t["sma50"] and t["close"] > t["sma50"] else 0
        m += 3 if t["r21"] > 0 else 0
        pts["momentum"] = m
        r = t["r21"]
        pts["room"] = 10 if r <= .05 else 7 if r <= .10 else 4 if r <= .20 else 1
    else:
        pts["momentum"] = pts["room"] = 0
    raw = sum(pts.values())
    pts["raw"] = raw
    pts["total"] = round(raw * 100 / 85)
    return pts


def bucket(f, pts, events180):
    """A / B / D / None plus exclusion reason."""
    gov = [e for e in events180 if e.get("governance")]
    if gov:
        return None, "governance flag: " + gov[0]["kw"]
    weak = f.get("loss") or (f.get("pat_yoy") is not None and f["pat_yoy"] < 0)
    if not weak and pts["total"] >= 75 and pts["growth"] >= 7 and pts["history"] >= 7:
        return "A", None
    if not weak and pts["total"] >= 65:
        return "B", None
    if weak:
        evidence = (f.get("rev_qoq") or 0) > 0 and (pts["momentum"] >= 7 or pts["positives"] >= 6)
        if evidence:
            return "D", None
        return None, "weak last quarter without hard recovery evidence"
    return None, f"score {pts['total']} below 65"


def signal_from(bucket_, pts, f):
    if bucket_ == "A":
        return "STRONG BUY"
    if bucket_ == "B":
        return "BUY"
    if bucket_ == "D":
        return "BUY (RISKY)"
    if pts["total"] < 25:
        return "STRONG SELL"
    if pts["total"] < 35 and pts["momentum"] <= 3:
        return "SELL"
    return None


def levels(sig, t):
    """Entry = last close (CMP). Targets / stops from 14-day ATR."""
    if not t or not t.get("atr"):
        return None
    c, a = t["close"], t["atr"]
    k = 3 if sig.startswith("STRONG") else 2
    rnd = lambda x: round(round(x / 0.05) * 0.05, 2)
    if "BUY" in sig or sig == "WATCH UP":
        return {"cmp": rnd(c), "entry": rnd(c), "target": rnd(c + k * a), "stop": rnd(c - 1.5 * a)}
    return {"cmp": rnd(c), "entry": rnd(c), "target": rnd(c - k * a), "stop": rnd(c + 1.5 * a)}


def bullets(f, pts, t, events90, name_date=None):
    b = []
    py, ry = f.get("pat_yoy"), f.get("rev_yoy")
    qlabel = quarter_label(f["qe"])
    if py is not None or ry is not None:
        b.append(f"Last quarter ({qlabel}) profit {pct(py)} YoY, revenue {pct(ry)} YoY (exchange filing)")
    elif f.get("loss"):
        b.append(f"Last quarter ({qlabel}) was loss-making (exchange filing)")
    b.append(f"Revenue Rs.{inr(f.get('rev'))} cr, profit Rs.{inr(f.get('pat'))} cr in {qlabel}"
             + (f"; revenue {pct(f['rev_qoq'])} QoQ" if f.get("rev_qoq") is not None else ""))
    pos = [e for e in events90 if e["sent"] == "pos"]
    neg = [e for e in events90 if e["sent"] == "neg"]
    if t and t["r21"] > .15:
        b.append(f"Risk: stock {pct(t['r21'])} in the last month, part of the good news may be priced in")
    elif neg:
        b.append(f"Risk: {neg[0]['line']}")
    elif pos:
        b.append(f"Catalyst: {pos[0]['line']}")
    elif t:
        b.append(f"Stock {pct(t['r21'])} in the last month, {'above' if t['sma50'] and t['close'] > t['sma50'] else 'below'} its 50-day average")
    return b[:3]


def quarter_label(qe_iso):
    d = dt.date.fromisoformat(qe_iso)
    fy = d.year + 1 if d.month > 3 else d.year
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month, 0)
    return f"Q{q} FY{str(fy)[2:]}"


def sector_relative(industry, ind_of, rets_at, date_iso):
    """Median 21-day return of industry peers minus median of all F&O stocks, as of date."""
    allr = [r for s, r in rets_at(date_iso).items() if r is not None]
    peers = [r for s, r in rets_at(date_iso).items() if r is not None and ind_of.get(s) == industry]
    if len(peers) < 3 or len(allr) < 20:
        return None
    return st.median(peers) - st.median(allr)
