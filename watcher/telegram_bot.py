"""Long-polls Telegram for button taps (no public URL needed) and drives SQLite + n8n."""
import json
import logging
import os
import re
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
    action, _, rest = data.partition(":")
    sid, _, arg = rest.partition(":")
    rows = db.query("SELECT status FROM clips WHERE short_id=?", (sid,))
    if action not in ("ok", "no", "recut", "fr", "fl", "bk") or not rows:
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text="Clip desconocido")
        return
    if rows[0]["status"] != "awaiting_approval":
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text=f"Ya está: {rows[0]['status']}")
        return
    msg = cq["message"]
    if action in ("fr", "bk"):                       # open / close the framing menu (no state change)
        from watcher import openshorts_client as osc, publishing
        if action == "fr":
            kb = {"inline_keyboard": [[{"text": lbl, "callback_data": f"fl:{sid}:{k}"}] for k, (lbl, _) in osc.FRAMINGS.items()]
                  + [[{"text": "⬅️ Volver", "callback_data": f"bk:{sid}"}]]}
        else:
            kb = json.loads(publishing.clip_meta(db, sid)["telegram_keyboard"])
        _tg("answerCallbackQuery", callback_query_id=cq["id"])
        _tg("editMessageReplyMarkup", chat_id=CHAT, message_id=msg["message_id"], reply_markup=kb)
        return
    if action == "recut":                            # ask for the changes as free text (reply to this message)
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text="Escribe los cambios")
        _tg("sendMessage", chat_id=CHAT, reply_markup={"force_reply": True, "selective": True,
            "input_field_placeholder": "p.ej. sin música, empieza 5s más tarde, encuadre horizontal"},
            text=f"✏️ Rehacer clip {sid}: responde a este mensaje con los cambios que quieres "
                 "(encuadre, subtítulos, gancho, duración, recortar inicio/final, más/menos sonido del juego). "
                 "Escribe «-» para rehacerlo sin cambios.")
        return
    if action == "fl":                               # re-render with the chosen framing
        from watcher import openshorts_client as osc
        if arg not in osc.FRAMINGS:
            _tg("answerCallbackQuery", callback_query_id=cq["id"], text="Encuadre desconocido")
            return
        db.execute("UPDATE clips SET layout=?, status='queued' WHERE short_id=?", (arg, sid))
        label = f"🖼 REHACIENDO con encuadre: {osc.FRAMINGS[arg][0]}"
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text=label)
        _tg("editMessageCaption", chat_id=CHAT, message_id=msg["message_id"],
            caption=(msg.get("caption", "") + f"\n\n{label}")[:1024], reply_markup={"inline_keyboard": []})
        log.info("clip %s -> reframe %s", sid, arg)
        return
    label = {"ok": "✅ APROBADO — se publicará tras una espera aleatoria", "no": "⏭ DESCARTADO", "recut": "🔁 REHACIENDO"}[action]
    db.set_status(sid, {"ok": "approved", "no": "skipped", "recut": "queued"}[action])
    _tg("answerCallbackQuery", callback_query_id=cq["id"], text=label)
    _tg("editMessageCaption", chat_id=CHAT, message_id=msg["message_id"],
        caption=(msg.get("caption", "") + f"\n\n{label}")[:1024], reply_markup={"inline_keyboard": []})
    if action == "ok":
        notify_n8n("clip-approved", sid)
    log.info("clip %s -> %s", sid, action)


def handle_message(db, msg: dict):
    """Reply to the 'Rehacer' force-reply prompt -> parse the request, store it, requeue the clip."""
    if str(msg.get("chat", {}).get("id")) != CHAT or not msg.get("text"):
        return
    m = re.search(r"Rehacer clip ([a-z0-9]{6})", (msg.get("reply_to_message") or {}).get("text", ""))
    if not m:
        return
    sid, text = m[1], msg["text"].strip()
    rows = db.query("SELECT status FROM clips WHERE short_id=?", (sid,))
    if not rows or rows[0]["status"] != "awaiting_approval":
        _tg("sendMessage", chat_id=CHAT, text=f"Clip {sid}: ya no está pendiente ({rows[0]['status'] if rows else 'desconocido'}).")
        return
    from watcher import llm
    params = {} if text in ("-", "–") else llm.parse_recut(text)
    fallback = params.pop("fallback", False)
    unapplied = params.pop("unapplied", None)
    db.execute("UPDATE clips SET recut_prompt=?, recut_params=?, status='queued' WHERE short_id=?",
               (text, json.dumps(params), sid))
    if params.get("layout"):
        db.execute("UPDATE clips SET layout=? WHERE short_id=?", (params["layout"], sid))
    names = {"layout": "encuadre", "hook_text": "gancho", "auto_hook": "gancho en pantalla", "captions": "subtítulos",
             "clip_min": "duración mín (s)", "clip_max": "duración máx (s)", "trim_start": "quitar del inicio (s)",
             "trim_end": "quitar del final (s)", "audio": "audio"}
    lines = [f"🔁 Rehaciendo {sid}" + (" (sin cambios)" if not params and not unapplied else ":")]
    lines += [f"• {names[k]}: {v}" for k, v in params.items()]
    if unapplied:
        lines.append(f"⚠️ No puedo aplicar: {unapplied}")
    if fallback:
        lines.append("⚠️ El LLM no respondió; solo entendí palabras clave simples. Si falta algo, vuelve a pulsar Rehacer cuando se recupere.")
    _tg("sendMessage", chat_id=CHAT, text="\n".join(lines))
    log.info("clip %s recut requested: %s", sid, params)


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
            r = _tg("getUpdates", offset=str(offset), timeout="30", allowed_updates=["callback_query", "message"])
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        handle_message(db, u["message"])
                    except Exception:
                        log.exception("message handler failed")
                if "callback_query" in u:
                    try:
                        handle_callback(db, u["callback_query"])
                    except Exception:
                        log.exception("callback failed")
        except Exception as e:
            log.warning("poll error: %s", e)
            time.sleep(5)
