"""Telegram message helper for publishers (plain urllib; no-op when Telegram is unconfigured)."""
import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("notify")


def send(text, reply_markup=None, env=None) -> bool:
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("telegram not configured; dropped: %s", text[:80])
        return False
    params = {"chat_id": chat, "text": text[:4096], "disable_web_page_preview": "false"}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", urllib.parse.urlencode(params).encode()), timeout=30).read()
        return True
    except Exception as e:
        log.warning("telegram send failed: %s", e)
        return False
