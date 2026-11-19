"""NSE public data client with disk cache and polite rate limiting.

Every number the radar shows comes from one of these exchange sources.
"""
import csv
import hashlib
import io
import json
import re
import threading
import time
import datetime as dt
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BASE = "https://www.nseindia.com"
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
FOREVER = None


def parse_date(s, fmts=("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d")):
    if not s:
        return None
    s = s.strip()
    for f in fmts:
        try:
            return dt.datetime.strptime(s.title() if "%b" in f else s, f)
        except ValueError:
            continue
    return None


class NSE:
    def __init__(self, cache_dir: Path, min_gap=0.35):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_gap = min_gap
        self._last = 0.0
        self._lock = threading.Lock()
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json,text/csv,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE + "/",
        })
        self.requests_made = 0

    # ---------- low level ----------
    def _cache_path(self, key):
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def _cache_get(self, key, ttl):
        p = self._cache_path(key)
        if not p.exists():
            return None
        try:
            obj = json.loads(p.read_text())
        except Exception:
            return None
        if ttl is not FOREVER and time.time() - obj["t"] > ttl:
            return None
        return obj["v"]

    def _cache_put(self, key, value):
        p = self._cache_path(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"t": time.time(), "v": value}))
        tmp.replace(p)

    def _fetch(self, url, as_json=True, timeout=40):
        err = None
        for attempt in range(4):
            with self._lock:
                gap = time.time() - self._last
                if gap < self.min_gap:
                    time.sleep(self.min_gap - gap)
                self._last = time.time()
            try:
                r = self.s.get(url, timeout=timeout)
                self.requests_made += 1
                if r.status_code == 200:
                    return r.json() if as_json else r.content
                err = f"HTTP {r.status_code}"
                if r.status_code in (401, 403, 429, 503):
                    time.sleep(2 + attempt * 3)
                    continue
                if r.status_code == 404:
                    break
            except (requests.RequestException, ValueError) as e:
                err = str(e)
                time.sleep(1 + attempt * 2)
        raise RuntimeError(f"NSE fetch failed ({err}): {url}")

    def get_json(self, url, ttl):
        v = self._cache_get(url, ttl)
        if v is not None:
            return v
        v = self._fetch(url, as_json=True)
        self._cache_put(url, v)
        return v

    def get_text(self, url, ttl):
        v = self._cache_get(url, ttl)
        if v is not None:
            return v
        v = self._fetch(url, as_json=False).decode("utf-8", "replace")
        self._cache_put(url, v)
        return v

    # ---------- universe ----------
    def fo_symbols(self):
        t = self.get_text("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", 86400)
        out = {}
        for row in csv.reader(io.StringIO(t)):
            if len(row) < 2:
                continue
            name, sym = row[0].strip(), row[1].strip()
            if not sym or sym == "SYMBOL" or sym in INDEX_UNDERLYINGS or name.upper().startswith("NIFTY"):
                continue
            out[sym] = name
        return out

    def industries(self):
        out = {}
        for lst in ("ind_nifty500list.csv", "ind_niftymicrocap250_list.csv"):
            try:
                v = self._cache_get("niftyindices:" + lst, 7 * 86400)
                if v is None:
                    r = requests.get("https://niftyindices.com/IndexConstituent/" + lst,
                                     headers={"User-Agent": UA}, timeout=30)
                    r.raise_for_status()
                    v = r.text
                    self._cache_put("niftyindices:" + lst, v)
                for row in csv.DictReader(io.StringIO(v)):
                    sym = (row.get("Symbol") or "").strip()
                    if sym and sym not in out:
                        out[sym] = {"industry": (row.get("Industry") or "Other").strip(),
                                    "name": (row.get("Company Name") or "").strip()}
            except Exception:
                continue
        return out

    # ---------- events ----------
    def board_meetings(self):
        """Upcoming board meetings from two NSE feeds (board-meetings + event calendar)."""
        rows = []
        try:
            for x in self.get_json(BASE + "/api/corporate-board-meetings?index=equities", 3600):
                rows.append({"symbol": x.get("bm_symbol"), "date": x.get("bm_date"),
                             "purpose": x.get("bm_purpose") or "", "desc": x.get("bm_desc") or "",
                             "source": "NSE board-meeting intimation"})
        except RuntimeError:
            pass
        try:
            for x in self.get_json(BASE + "/api/event-calendar", 3600):
                rows.append({"symbol": x.get("symbol"), "date": x.get("date"),
                             "purpose": x.get("purpose") or "", "desc": x.get("bm_desc") or "",
                             "source": "NSE event calendar"})
        except RuntimeError:
            pass
        return rows

    def announcements(self, frm: dt.date, to: dt.date, symbol=None, ttl=1800):
        url = (BASE + "/api/corporate-announcements?index=equities"
               f"&from_date={frm:%d-%m-%Y}&to_date={to:%d-%m-%Y}")
        if symbol:
            url += "&symbol=" + requests.utils.quote(symbol)
        old = to < dt.date.today() - dt.timedelta(days=2)
        data = self.get_json(url, FOREVER if old else ttl)
        keep = ("an_dt", "desc", "attchmntText", "symbol", "sm_name", "smIndustry", "attchmntFile")
        return [{k: x.get(k) for k in keep} for x in (data or [])]

    # ---------- results ----------
    def filings(self, symbol, ttl=6 * 3600):
        url = (BASE + "/api/integrated-filing-results?index=equities"
               "&type=Integrated%20Filing-%20Financials&symbol=" + requests.utils.quote(symbol))
        data = self.get_json(url, ttl)
        rows = data.get("data", []) if isinstance(data, dict) else []
        by_q = {}
        for x in rows:
            qe = parse_date(x.get("qe_Date"))
            when = parse_date(x.get("broadcast_Date")) or parse_date(x.get("creation_Date"))
            if not qe or not when or not x.get("xbrl"):
                continue
            cons = (x.get("consolidated") or "").lower().startswith("consol")
            item = {"qe": qe.date().isoformat(), "when": when.isoformat(), "xbrl": x["xbrl"],
                    "consolidated": cons, "name": x.get("cmName") or x.get("smName")}
            cur = by_q.get(item["qe"])
            # prefer consolidated; keep the earliest broadcast time for the quarter as the event time
            if cur is None:
                by_q[item["qe"]] = item
            else:
                first = min(cur["when"], item["when"])
                if item["consolidated"] and not cur["consolidated"]:
                    cur = item
                cur["when"] = first
                by_q[item["qe"]] = cur
        return sorted(by_q.values(), key=lambda r: r["qe"], reverse=True)

    def xbrl_numbers(self, url):
        key = "xbrlnum:" + url
        v = self._cache_get(key, FOREVER)
        if v is not None:
            return v
        t = self._fetch(url, as_json=False, timeout=60).decode("utf-8", "replace")

        def g(tag):
            m = re.search(r'<in-capmkt:%s contextRef="OneD"[^>]*>([^<]+)<' % tag, t)
            if not m:
                return None
            try:
                return float(m.group(1))
            except ValueError:
                return None

        def gi(tag):  # instant/any context fallback
            m = re.search(r'<in-capmkt:%s contextRef="[^"]*"[^>]*>([^<]+)<' % tag, t)
            try:
                return float(m.group(1)) if m else None
            except ValueError:
                return None

        rev = g("RevenueFromOperations")
        if rev is None:  # banks / NBFC formats
            rev = g("InterestEarned") or g("TotalIncome") or g("Income")
        pat = g("ProfitLossForPeriod")
        if pat is None:
            pat = g("ProfitLossForThePeriod") or g("NetProfitLossForThePeriod")
        v = {
            "rev_cr": rev / 1e7 if rev is not None else None,
            "pat_cr": pat / 1e7 if pat is not None else None,
            "eps": g("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations")
                   or g("BasicEarningsLossPerShareFromContinuingOperations"),
            "paidup": g("PaidUpValueOfEquityShareCapital") or gi("PaidUpValueOfEquityShareCapital"),
            "fv": g("FaceValueOfEquityShareCapital") or gi("FaceValueOfEquityShareCapital"),
        }
        self._cache_put(key, v)
        return v

    # ---------- prices ----------
    def _hist_block(self, symbol, start: dt.date, end: dt.date):
        today = dt.date.today()
        url = (BASE + "/api/historicalOR/generateSecurityWiseHistoricalData"
               f"?from={start:%d-%m-%Y}&to={end:%d-%m-%Y}&symbol={requests.utils.quote(symbol)}"
               "&type=priceVolumeDeliverable&series=ALL")
        ttl = FOREVER if end < today - dt.timedelta(days=3) else 4 * 3600
        data = self.get_json(url, ttl)
        return data.get("data", []) if isinstance(data, dict) else []

    def history(self, symbol, start: dt.date, end: dt.date = None):
        """Daily bars, oldest first. Blocks are aligned to 2-month calendar windows so they cache."""
        end = end or dt.date.today()
        bars = {}
        y, m = start.year, start.month - ((start.month - 1) % 2)
        while True:
            b0 = dt.date(y, m, 1)
            if b0 > end:
                break
            ny, nm = (y, m + 2) if m <= 10 else (y + 1, m - 10)
            b1 = min(dt.date(ny, nm, 1) - dt.timedelta(days=1), end)
            for r in self._hist_block(symbol, b0, b1):
                d = parse_date(r.get("mTIMESTAMP"))
                if not d:
                    continue
                series = r.get("CH_SERIES")
                key = d.date()
                if key in bars and bars[key]["series"] == "EQ":
                    continue
                if series not in ("EQ", "BE", "BZ", "SM", "ST") and key in bars:
                    continue
                bars[key] = {"d": key.isoformat(), "series": series,
                             "o": r.get("CH_OPENING_PRICE"), "h": r.get("CH_TRADE_HIGH_PRICE"),
                             "l": r.get("CH_TRADE_LOW_PRICE"), "c": r.get("CH_CLOSING_PRICE"),
                             "pc": r.get("CH_PREVIOUS_CLS_PRICE"), "v": r.get("CH_TOT_TRADED_QTY")}
            y, m = ny, nm
        out = [bars[k] for k in sorted(bars) if start <= k <= end]
        return [b for b in out if b["c"] and b["pc"]]


def _live_quote(self, symbol, ttl=60):
    """Live/last quote from NSE's NextApi (works when quote-equity is blocked)."""
    url = ("https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData"
           "&marketType=N&series=EQ&symbol=" + requests.utils.quote(symbol))
    j = self.get_json(url, ttl)
    try:
        e = j["equityResponse"][0]
    except (KeyError, IndexError, TypeError):
        return None
    md, ti, si = e.get("metaData", {}), e.get("tradeInfo", {}), e.get("secInfo", {})
    last = ti.get("lastPrice") or md.get("closePrice")
    prev = md.get("previousClose")
    return {"last": last, "prev": prev, "chg": (last / prev - 1) if last and prev else None,
            "open": md.get("open") or None,
            "high": md.get("dayHigh"), "low": md.get("dayLow"), "vol": ti.get("totalTradedVolume"),
            "mcap_cr": (ti.get("totalMarketCap") or 0) / 1e7 or None, "shares": ti.get("issuedSize"),
            "industry": si.get("basicIndustry"), "time": e.get("lastUpdateTime")}


NSE.live_quote = _live_quote


def _fo_oi(self, d_iso):
    """Per-symbol stock-futures open interest and its change (all expiries summed) for one session.
    Source: NSE F&O bhavcopy. Returns {sym: [oi, change_in_oi]} or None if the file is not published yet."""
    import zipfile
    from collections import defaultdict
    key = f"fo_oi:{d_iso}"
    v = self._cache_get(key, FOREVER)
    if v is not None:
        return v
    url = ("https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_"
           f"{d_iso.replace('-', '')}_F_0000.csv.zip")
    try:
        b = self._fetch(url, as_json=False, timeout=90)
    except RuntimeError:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    oi, ch = defaultdict(float), defaultdict(float)
    for r in csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode())):
        if r["FinInstrmTp"] != "STF":
            continue
        oi[r["TckrSymb"]] += float(r["OpnIntrst"] or 0)
        ch[r["TckrSymb"]] += float(r["ChngInOpnIntrst"] or 0)
    v = {s: [oi[s], ch[s]] for s in oi}
    self._cache_put(key, v)
    return v


def _fo_movers(self, ttl=120):
    """Live top-20 F&O gainers and losers from NSE (market hours)."""
    out = {}
    for k, idx in (("gainers", "gainers"), ("losers", "loosers")):
        j = self.get_json(f"https://www.nseindia.com/api/live-analysis-variations?index={idx}", ttl)
        blk = (j or {}).get("FOSec", {})
        out[k] = [{"sym": r["symbol"], "chg": r["perChange"] / 100, "ltp": r["ltp"]} for r in blk.get("data", [])]
        out["time"] = blk.get("timestamp")
    return out


NSE.fo_oi = _fo_oi
NSE.fo_movers = _fo_movers
