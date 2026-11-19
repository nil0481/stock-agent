import hashlib
import json
import os
import re
import threading
from pathlib import Path

import requests

NUM = re.compile(r"\d+(?:[.,]\d+)*")
CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "llm_cache.json"
PROMPT_VERSION = "v2"
_lock = threading.Lock()
_cache = None
STATS = {"calls": 0, "cached": 0, "prompt_tokens": 0, "completion_tokens": 0}


def enabled():
    return bool(os.getenv("FREEMODEL_API_KEY"))


def reset_stats():
    for k in STATS:
        STATS[k] = 0


def _model():
    return os.getenv("FREEMODEL_MODEL", "gpt-6-luna")


def _key(kind, text):
    return hashlib.sha1(f"{PROMPT_VERSION}|{_model()}|{kind}|{text}".encode()).hexdigest()


def _load():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text())
        except Exception:
            _cache = {}
    return _cache


def _get(key):
    with _lock:
        return _load().get(key)


def _put(key, value):
    with _lock:
        c = _load()
        c[key] = value
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(c))
        tmp.replace(CACHE_FILE)


def _chat(messages, max_tokens=600):
    base = os.getenv("FREEMODEL_BASE_URL", "https://freemodel.in/v1").rstrip("/")
    r = requests.post(
        base + "/chat/completions",
        headers={"Authorization": "Bearer " + os.environ["FREEMODEL_API_KEY"],
                 "Content-Type": "application/json"},
        json={"model": _model(), "messages": messages, "temperature": 0.1, "max_tokens": max_tokens},
        timeout=90,
    )
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    STATS["calls"] += 1
    STATS["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
    STATS["completion_tokens"] += int(u.get("completion_tokens") or 0)
    return j["choices"][0]["message"]["content"].strip()


def _numbers_ok(out, src):
    have = {n.replace(",", "") for n in NUM.findall(src)}
    for n in NUM.findall(out):
        n = n.replace(",", "")
        if n not in have and len(n) > 1:
            return False
    return True


SYSTEM = ("You summarise Indian stock-exchange filings for a personal research dashboard. "
          "Use ONLY the facts given. Never add numbers, dates, prices or claims that are not in the input. "
          "Plain English, no disclaimers, write 'Rs.' for rupees.")

JUDGE = ("For each filing return: line = one line (max 20 words) saying what happened and why it matters to "
         "the stock; impact = positive, negative or neutral for the share price; material = true only if it "
         "could move the share price (routine compliance letters, presentations, small fines/refunds, "
         "treasury investments and procedural SPVs are NOT material). "
         "Return only a JSON list of {\"id\":..., \"line\":..., \"impact\":..., \"material\":...}.\n\n")


def one_liners(items):
    """items: [{id, text}] -> {id: {line, impact, material}}. Each filing text is sent once, ever."""
    if not enabled() or not items:
        return {}
    out, todo = {}, []
    for x in items:
        text = x["text"][:1500]
        hit = _get(_key("judge", text))
        if hit is not None:
            STATS["cached"] += 1
            out[x["id"]] = hit
        else:
            todo.append({"id": x["id"], "text": text})
    obj = re.compile(r"\{[^{}]*\}")
    for i in range(0, len(todo), 5):
        chunk = todo[i:i + 5]
        prompt = JUDGE + json.dumps(chunk, ensure_ascii=False)
        try:
            txt = _chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], 1800)
        except Exception:
            continue
        for raw in obj.findall(txt):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            rid = str(row.get("id", "")).strip()
            src = next((x["text"] for x in chunk if str(x["id"]) == rid), "")
            if not src:
                continue
            line = (row.get("line") or "").strip()
            impact = str(row.get("impact", "")).lower()
            material = row.get("material")
            if isinstance(material, str):
                material = material.strip().lower() == "true"
            res = {"line": line if line and _numbers_ok(line, src) else None,
                   "impact": impact if impact in ("positive", "negative", "neutral") else None,
                   "material": material if isinstance(material, bool) else None}
            out[rid] = res
            if res["impact"]:
                _put(_key("judge", src), res)
    return out


def narrative(facts_text):
    """2-4 sentence 'why it moved' from a stable facts block. Cached until the facts change."""
    if not enabled():
        return None
    k = _key("story", facts_text)
    hit = _get(k)
    if hit is not None:
        STATS["cached"] += 1
        return hit
    try:
        txt = _chat([{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": "Explain in 2-4 short sentences what triggered the big moves "
                                                 "and the lesson for spotting similar setups. Facts:\n" + facts_text}])
    except Exception:
        return None
    if not _numbers_ok(txt, facts_text):
        return None
    _put(k, txt)
    return txt
