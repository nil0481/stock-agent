import json
import os
import sys
import threading
import time
import traceback
import datetime as dt
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
import job

PORT = int(os.getenv("PORT", "8379"))
HOST = os.getenv("HOST", "127.0.0.1")
RUN_AT = os.getenv("DAILY_RUN_IST", "08:30,09:20")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

state = {"running": False, "message": "idle", "started": None, "finished": None, "error": None,
         "last_trigger": None, "next_auto": None}
lock = threading.Lock()


def run_job(trigger, telegram=False):
    with lock:
        if state["running"]:
            return False
        state.update(running=True, error=None, started=dt.datetime.now(IST).strftime("%H:%M:%S"),
                     last_trigger=trigger, message="starting")

    def work():
        try:
            r = job.main(send=telegram, progress=lambda m: state.update(message=m))
            tg = r.get("telegram")
            state["telegram"] = ("sent" if tg and all(x == "200" for x in tg) else f"failed {tg}") if tg else "not sent"
            state["message"] = "done"
        except Exception as e:
            state["error"] = f"{e.__class__.__name__}: {e}"
            state["message"] = "failed"
            traceback.print_exc()
        finally:
            state["running"] = False
            state["finished"] = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    threading.Thread(target=work, daemon=True).start()
    return True


def scheduler():
    """Runs at every time in DAILY_RUN_IST (comma separated). Runs after 09:00 are skipped on weekends
    because they exist to confirm movers with live prices."""
    times = sorted(tuple(map(int, t.strip().split(":"))) for t in RUN_AT.split(",") if t.strip())
    while True:
        now = dt.datetime.now(IST)
        cands = []
        for day in range(0, 4):
            for hh, mm in times:
                t = (now + dt.timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if t <= now or ((hh, mm) > (9, 0) and t.weekday() >= 5):
                    continue
                cands.append(t)
        nxt = min(cands)
        state["next_auto"] = nxt.strftime("%Y-%m-%d %H:%M IST")
        while dt.datetime.now(IST) < nxt:
            time.sleep(20)
        label = nxt.strftime("auto %H:%M IST")
        if not run_job(label, telegram=True):
            state["error"] = f"{label} run skipped: a refresh was already running"
        time.sleep(61)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/status":
            return self._json(state)
        if p in ("/", "/index.html"):
            self.path = "/static/index.html"
        elif p.startswith("/static/") or p == "/data/radar.json":
            pass
        else:
            return self.send_error(404)
        return super().do_GET()

    def end_headers(self):
        if self.path.endswith(".json"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):
        if self.path.split("?")[0] == "/api/refresh":
            started = run_job("manual refresh", telegram=True)
            return self._json({"started": started, "state": state}, 202 if started else 409)
        self.send_error(404)


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Pre-Results Radar on http://{HOST}:{PORT}  (auto run {RUN_AT} IST)", flush=True)
    srv.serve_forever()
