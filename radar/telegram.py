import os
import requests


def enabled():
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send(text):
    """Send HTML text, split into Telegram-sized chunks. Returns list of status strings."""
    if not enabled():
        return ["telegram not configured"]
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    status = []
    for c in chunks:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        status.append(f"{r.status_code}")
    return status
