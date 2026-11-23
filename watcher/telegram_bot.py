"""Long-polls Telegram for button taps (no public URL needed) and drives SQLite + n8n."""
import json
import logging
import os
import time
import urllib.parse
import urllib.request

log = logging.getLogger("telegram")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
N8N = os.environ.get("N8N_INTERNAL_URL", "http://n8n:5678")


def _tg(method, **params):
    data = urllib.parse.urlencode({k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}).encode()
    with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data), timeout=60) as r:
        return json.load(r)


def notify_n8n(path: str, short_id: str):
    req = urllib.request.Request(f"{N8N}/webhook/{path}", json.dumps({"short_id": short_id}).encode(),
                                 {"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


def handle_callback(db, cq: dict):
    data = cq.get("data", "")
    if str(cq.get("message", {}).get("chat", {}).get("id")) != CHAT:
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text="No autorizado")
        return
    action, _, sid = data.partition(":")
    rows = db.query("SELECT status FROM clips WHERE short_id=?", (sid,))
    if action not in ("ok", "no", "recut") or not rows:
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text="Clip desconocido")
        return
    if rows[0]["status"] != "awaiting_approval":
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text=f"Ya está: {rows[0]['status']}")
        return
    label = {"ok": "✅ APROBADO — se publicará tras una espera aleatoria", "no": "⏭ DESCARTADO", "recut": "🔁 REHACIENDO"}[action]
    db.set_status(sid, {"ok": "approved", "no": "skipped", "recut": "queued"}[action])
    _tg("answerCallbackQuery", callback_query_id=cq["id"], text=label)
    msg = cq["message"]
    _tg("editMessageCaption", chat_id=CHAT, message_id=msg["message_id"],
        caption=(msg.get("caption", "") + f"\n\n{label}")[:1024], reply_markup={"inline_keyboard": []})
    if action == "ok":
        notify_n8n("clip-approved", sid)
    log.info("clip %s -> %s", sid, action)


def poll_forever(db):
    if not TOKEN or not CHAT:
        log.warning("telegram not configured; poller off")
        return
    try:
        _tg("deleteWebhook")
    except Exception as e:
        log.warning("deleteWebhook: %s", e)
    offset = 0
    while True:
        try:
            r = _tg("getUpdates", offset=str(offset), timeout="30", allowed_updates=["callback_query"])
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    try:
                        handle_callback(db, u["callback_query"])
                    except Exception:
                        log.exception("callback failed")
        except Exception as e:
            log.warning("poll error: %s", e)
            time.sleep(5)
