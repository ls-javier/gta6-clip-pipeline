"""Clip metadata/captions, Upload-Post publishing and 48h disk cleanup."""
import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("publishing")
UP_BASE = "https://api.upload-post.com/api"
DRY_RUN = lambda: os.environ.get("PUBLISH_DRY_RUN", "true").lower() != "false"
ALL_PLATFORMS = ("tiktok", "instagram", "youtube")
OUTPUT_DIR = Path(os.environ.get("OPENSHORTS_OUTPUT", "/data/output"))


def _up_headers():
    return {"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}"}


def clip_meta(db, short_id: str) -> dict:
    rows = db.query("SELECT c.*, s.twitch_channel, s.credit_handle, s.campaign_name, s.required_hashtags, "
                    "s.ad_disclosure_text FROM clips c JOIN streamers s ON s.id=c.streamer_id WHERE c.short_id=?", (short_id,))
    if not rows:
        raise KeyError(short_id)
    c = rows[0]
    tags = " ".join(t if t.startswith("#") else "#" + t
                    for t in ((c["required_hashtags"] or "") + " " + (c.get("hashtags") or "")).replace(",", " ").split())
    handle = c["credit_handle"] if c["credit_handle"].startswith("@") else "@" + c["credit_handle"]
    hook = c["hook_text"] or "GTA 6 moment"
    base_tags = " ".join(dict.fromkeys(f"#GTA6 {tags}".split()))
    credit = f"Clip de {handle} — https://twitch.tv/{c['twitch_channel']}"
    disclosure = c["ad_disclosure_text"] or ""
    caption = "\n".join(x for x in (hook, credit, disclosure, base_tags) if x)
    yt_credit = f"Clip de {handle}"          # YouTube: no external links in descriptions
    yt_description = "\n".join(x for x in (hook, yt_credit, disclosure, base_tags) if x)
    yt_title = f"{hook} {handle} #Shorts"[:100]
    checklist = ("REVISA ANTES DE APROBAR:\n"
                 "☐ Sin gore explícito en los primeros 7 s\n☐ No es una cinemática / TV o radio del juego\n"
                 "☐ No se oye música\n☐ Se ve la reacción del streamer / cámara")
    tg = f"🎬 {c['short_id']} · {c['twitch_channel']} · pico {c['spike_ratio'] or 0:.1f}x\nGancho: {hook}\n\n{checklist}"
    keyboard = {"inline_keyboard": [[{"text": "✅ Aprobar", "callback_data": f"ok:{short_id}"},
                                     {"text": "⏭ Descartar", "callback_data": f"no:{short_id}"},
                                     {"text": "🔁 Rehacer", "callback_data": f"recut:{short_id}"}],
                                    [{"text": "🖼 Cambiar encuadre", "callback_data": f"fr:{short_id}"}]]}
    assert all(len(b["callback_data"].encode()) <= 64 for r in keyboard["inline_keyboard"] for b in r)
    return {"short_id": short_id, "status": c["status"], "channel": c["twitch_channel"], "handle": handle,
            "hook": hook, "caption": caption, "yt_title": yt_title, "yt_description": yt_description,
            "telegram_caption": tg[:1024], "telegram_keyboard": json.dumps(keyboard),
            "master_path": c["master_916_path"], "preview_path": c["preview_480p_path"],
            "dry_run": DRY_RUN()}


def _multipart(fields, file_field, file_path, extra_files=()):
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{Path(file_path).name}"\r\n'
                 "Content-Type: video/mp4\r\n\r\n".encode())
    parts.append(Path(file_path).read_bytes())
    parts.append(b"\r\n")
    for name, fp, ctype in extra_files:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{name}"; filename="{Path(fp).name}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode())
        parts.append(Path(fp).read_bytes())
        parts.append(b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def _get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_up_headers()), timeout=timeout) as r:
        return json.load(r)


def connected_platforms() -> list:
    user = os.environ["UPLOAD_POST_USER"]
    j = _get(f"{UP_BASE}/uploadposts/users")
    for p in j.get("profiles", []):
        if p.get("username") == user:
            sa = p.get("social_accounts", {})
            return [k for k in ALL_PLATFORMS if sa.get(k)]
    raise RuntimeError(f"Upload-Post profile {user!r} not found")


def _find_result(status_json: dict, platform: str):
    res = status_json.get("results") or status_json.get("platforms") or status_json
    if isinstance(res, list):
        res = {r.get("platform"): r for r in res if isinstance(r, dict)}
    r = res.get(platform) if isinstance(res, dict) else None
    return r if isinstance(r, dict) else None


BACKEND = lambda: os.environ.get("PUBLISH_BACKEND", "hybrid").lower()


def _upload_one(meta: dict, platform: str, dry: bool) -> dict:
    rid = f"{meta['short_id']}-{platform}-{uuid.uuid4().hex[:6]}"
    title = meta["yt_title"] if platform == "youtube" else meta["caption"]
    fields = [("user", os.environ["UPLOAD_POST_USER"]), ("platform[]", platform), ("title", title),
              ("description", meta["yt_description"]), ("async_upload", "true"), ("request_id", rid)]
    if platform == "youtube":
        fields.append(("privacyStatus", "private" if dry else "public"))
        fields.append(("selfDeclaredMadeForKids", "false"))       # "Content for kids: No"
    if platform == "tiktok" and dry:
        fields.append(("privacy_level", "SELF_ONLY"))
    extra = []
    if platform == "youtube":                                        # cover = first frame (hook pill burned in)
        try:
            from watcher import media
            thumb = str(Path(meta["master_path"]).with_name(f"{meta['short_id']}_cover.jpg"))
            extra.append(("thumbnail", media.first_frame(meta["master_path"], thumb), "image/jpeg"))
        except Exception as e:
            log.warning("cover extraction failed, uploading without thumbnail: %s", e)
    body, ctype = _multipart(fields, "video", meta["master_path"], extra)
    req = urllib.request.Request(f"{UP_BASE}/upload", body, {**_up_headers(), "Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            first = json.load(r)
    except urllib.error.HTTPError as e:
        return {"success": False, "error": f"HTTP {e.code}: {e.read()[:300].decode('utf-8','replace')}"}
    log.info("upload-post %s submit: %s", platform, first)
    if first.get("results") and _find_result(first, platform):      # finished synchronously
        r = _find_result(first, platform)
        return {"success": bool(r.get("success")), "url": r.get("url"), "error": r.get("error")}
    rid = first.get("request_id") or rid
    for _ in range(60):                                              # up to 10 min
        time.sleep(10)
        try:
            st = _get(f"{UP_BASE}/uploadposts/status?request_id={rid}")
        except Exception as e:
            log.warning("status poll error: %s", e)
            continue
        r = _find_result(st, platform)
        if r and (r.get("success") is not None or r.get("url") or r.get("error")):
            log.info("upload-post %s result: %s", platform, st)
            return {"success": bool(r.get("success", bool(r.get("url")))), "url": r.get("url"), "error": r.get("error")}
        if st.get("status") in ("failed", "error"):
            return {"success": False, "error": json.dumps(st)[:300]}
    return {"success": False, "error": "timed out waiting for Upload-Post status"}


UP_MONTHLY_LIMIT = lambda: int(os.environ.get("UPLOAD_POST_MONTHLY_LIMIT", "10"))
_QUOTA_HINTS = ("upgrade_url", "monthly", "quota", "HTTP 429")


def upload_post_used_this_month(db, now=None) -> int:
    """Uploads Upload-Post has been given this calendar month that count against its free allowance.
    TikTok is excluded by default (it needs the paid Basic plan, which is unlimited)."""
    now = now or datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-01T00:00:00Z")
    platforms = ("tiktok", "instagram", "youtube") if os.environ.get("UPLOAD_POST_COUNT_TIKTOK", "false").lower() == "true" \
        else ("instagram", "youtube")
    q = ",".join("?" * len(platforms))
    return db.query(f"SELECT COUNT(*) n FROM publications WHERE via='upload_post' AND published_at>=? "
                    f"AND platform IN ({q})", (start, *platforms))[0]["n"]


def route_platforms(backend, wanted, up_avail, native_avail, used, limit, tiktok_route="native"):
    """Decide which service posts each platform. Returns ({platform: 'upload_post'|'native'}, {platform: reason}).

    hybrid: TikTok via the native inbox-draft uploader (free; a human taps Post in the app) unless
    TIKTOK_ROUTE=upload_post (paid Basic plan); YouTube/Instagram via Upload-Post until its monthly
    allowance is consumed, then via the native APIs."""
    routes, skipped = {}, {}
    if backend == "native":
        for p in wanted:
            (routes.__setitem__(p, "native") if p in native_avail else skipped.__setitem__(p, "credentials not set in .env"))
        return routes, skipped
    if backend != "hybrid":
        for p in wanted:
            (routes.__setitem__(p, "upload_post") if p in up_avail
             else skipped.__setitem__(p, "account not connected in Upload-Post"))
        return routes, skipped
    for p in wanted:
        if p == "tiktok":
            if tiktok_route == "upload_post":
                if p in up_avail:
                    routes[p] = "upload_post"
                else:
                    skipped[p] = "TikTok not connected in Upload-Post (needs Basic plan)"
            elif p in native_avail:
                routes[p] = "native"
            else:
                skipped[p] = "TikTok native credentials not set in .env (see TIKTOK_* )"
        elif p in up_avail and used < limit:
            routes[p] = "upload_post"
            used += 1
        elif p in native_avail:
            routes[p] = "native"
        elif p in up_avail:
            skipped[p] = f"Upload-Post monthly allowance ({limit}) used and native {p} credentials not set"
        else:
            skipped[p] = "not connected in Upload-Post and native credentials not set"
    return routes, skipped


def _looks_like_quota(err) -> bool:
    return any(h.lower() in str(err or "").lower() for h in _QUOTA_HINTS)


def publish(db, short_id: str) -> dict:
    meta = clip_meta(db, short_id)
    if meta["status"] != "approved":
        raise RuntimeError(f"clip {short_id} is {meta['status']}, not approved")
    if not meta["master_path"] or not Path(meta["master_path"]).exists():
        raise RuntimeError("master file missing")
    dry = DRY_RUN()
    backend = BACKEND()
    up_avail, native_avail, npub = [], [], None
    if backend in ("upload_post", "hybrid"):
        try:
            up_avail = connected_platforms()
        except Exception as e:
            if backend == "upload_post":
                raise
            log.warning("Upload-Post unavailable, native only: %s", e)
    if backend in ("native", "hybrid"):
        from watcher import native_publishers as npub
        native_avail = npub.configured_platforms()
    wanted = list(ALL_PLATFORMS)
    skipped = {}
    if dry and "instagram" in wanted:                # Instagram has no private visibility
        wanted.remove("instagram")
        skipped["instagram"] = "dry run: Instagram cannot post privately"
    routes, sk = route_platforms(backend, wanted, up_avail, native_avail,
                                 upload_post_used_this_month(db), UP_MONTHLY_LIMIT(),
                                 os.environ.get("TIKTOK_ROUTE", "native").lower())
    skipped.update(sk)
    urls, errors, notes, details = {}, {}, [], {}

    def run(p, via):
        if via == "native":
            try:
                r = npub.UPLOADERS[p][1](db, meta, dry)
            except Exception as e:
                log.exception("native %s upload failed", p)
                r = {"success": False, "error": str(e)[:400]}
            if r.get("note"):
                notes.append(f"ℹ️ {p}: {r['note']}")
            return r
        return _upload_one(meta, p, dry)

    for p, via in routes.items():
        r = run(p, via)
        if via == "upload_post" and not r.get("success") and p != "tiktok" and p in native_avail \
                and _looks_like_quota(r.get("error")):
            notes.append(f"ℹ️ {p}: Upload-Post allowance exhausted, retrying via native API")
            via, r = "native", run(p, "native")
        if r.get("success"):
            urls[p] = r.get("url") or ("(draft in TikTok app inbox)" if p == "tiktok" and via == "native"
                                       else "(posted; URL not returned)")
            details[p] = {"url": r.get("url"), "video_id": r.get("video_id"), "via": via}
            db.execute("INSERT OR REPLACE INTO publications (clip_short_id,platform,live_url,published_at,via) "
                       "VALUES (?,?,?,?,?)",
                       (short_id, p, r.get("url"), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), via))
        else:
            errors[p] = r.get("error") or "unknown error"
    db.set_status(short_id, "published" if urls else "failed")
    lines = [f"{'🧪 PRUEBA (privado) ' if dry else '🚀 PUBLICADO '}— clip {short_id}"]
    lines += [f"✅ {p}: {u}" for p, u in urls.items()]
    lines += [f"❌ {p}: {e}" for p, e in errors.items()]
    lines += [f"⏭ {p}: {e}" for p, e in skipped.items()]
    lines += notes
    if not dry and urls:
        lines.append("\nPega los enlaces en el portal de la campaña ahora (ventana típica de 30 min).")
    return {"urls": urls, "errors": errors, "skipped": skipped, "dry_run": dry, "routes": routes, "details": details,
            "message": "\n".join(lines)}


def cleanup(db, hours=48) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.query(
        "SELECT c.* FROM clips c WHERE (c.status='published' AND (SELECT MAX(published_at) FROM publications p "
        "WHERE p.clip_short_id=c.short_id) < ?) OR (c.status IN ('skipped','failed') AND c.created_at < ?)", (iso, iso))
    removed = 0
    for c in rows:
        for col in ("raw_path", "sanitized_path", "master_916_path", "preview_480p_path"):
            if c[col]:
                try:
                    Path(c[col]).unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
                db.execute(f"UPDATE clips SET {col}=NULL WHERE short_id=?", (c["short_id"],))
    jobs = 0
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.iterdir():
            if d.is_dir() and d.name != "thumbnails" and time.time() - d.stat().st_mtime > hours * 3600:
                shutil.rmtree(d, ignore_errors=True)
                jobs += 1
    return {"clips": len(rows), "files_removed": removed, "openshorts_job_dirs_removed": jobs}
