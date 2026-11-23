"""TikTok unaudited Inbox-draft uploader + video.list discovery (PUBLISH_BACKEND=native only).

The draft lands in the TikTok app inbox; the human pastes the caption and taps Post. We then poll
video.list every 3 min (max 30 min) to find the new public video's id/share_url.
"""
import os
import threading
import time
from pathlib import Path

from db import tokens
from publishers import notify
from publishers._http import ApiError, http as _default_http

API = "https://open.tiktokapis.com/v2"
MAX_SINGLE_CHUNK = 64 * 1024 * 1024
MAX_DRAFTS_PER_24H = 5
POLL_EVERY, POLL_FOR = 180, 1800


def configured(env=None) -> bool:
    env = os.environ if env is None else env
    return all(env.get(k) for k in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET")) and bool(env.get("TIKTOK_REFRESH_TOKEN"))


def drafts_last_24h(db, now=None) -> int:
    from datetime import datetime, timedelta, timezone
    since = ((now or datetime.now(timezone.utc)) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.query("SELECT COUNT(*) n FROM publications WHERE platform='tiktok' AND via='native' AND published_at>=?",
                    (since,))[0]["n"]


def upload(db, meta, dry, http=_default_http, token_http=tokens.default_http, env=None) -> dict:
    path = Path(meta["master_path"])
    size = path.stat().st_size
    if size > MAX_SINGLE_CHUNK:
        raise ApiError(f"master is {size/1e6:.0f} MB; single-chunk limit is 64 MB (chunked upload not implemented)")
    if drafts_last_24h(db) >= MAX_DRAFTS_PER_24H:
        return {"success": False, "url": None, "error": f"TikTok allows {MAX_DRAFTS_PER_24H} pending drafts per 24h"}
    token = tokens.get_valid_access_token(db, "tiktok", env, token_http)
    init = http("POST", f"{API}/post/publish/inbox/video/init/", data={
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}},
        headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if (init.get("error") or {}).get("code", "ok") != "ok":
        raise ApiError(f"TikTok init: {init['error']}")
    http("PUT", init["data"]["upload_url"], data=path.read_bytes(), headers={
        "Content-Type": "video/mp4", "Content-Length": str(size), "Content-Range": f"bytes 0-{size-1}/{size}"})
    note = ("draft is in your TikTok app inbox: open TikTok → Inbox, paste the caption below, optionally add a trending "
            f"sound, tap Post. I'll watch for the public URL for 30 min.\n\n{meta['caption']}")
    return {"success": True, "url": None, "error": None, "note": note}


def find_new_video(db, since_ts, known_ids, http=_default_http, token_http=tokens.default_http, env=None):
    """Newest public video created at/after since_ts whose id we haven't recorded, else None."""
    token = tokens.get_valid_access_token(db, "tiktok", env, token_http)
    j = http("POST", f"{API}/video/list/?fields=id,create_time,share_url,title,video_description",
             data={"max_count": 20}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    for v in sorted((j.get("data") or {}).get("videos") or [], key=lambda v: -v.get("create_time", 0)):
        if v.get("create_time", 0) >= since_ts and v["id"] not in known_ids:
            return v
    return None


def discover(db, short_id, since_ts, on_found, http=_default_http, token_http=tokens.default_http, env=None,
             sleep=time.sleep, poll_every=POLL_EVERY, poll_for=POLL_FOR) -> dict | None:
    """Poll until the human posts the draft. Calls on_found(video) once and returns it, or None on timeout."""
    known = {r["platform_video_id"] for r in db.query(
        "SELECT platform_video_id FROM publications_v2 WHERE platform='tiktok' AND platform_video_id IS NOT NULL")}
    waited = 0
    while waited <= poll_for:
        try:
            v = find_new_video(db, since_ts, known, http, token_http, env)
        except Exception:                            # transient API errors must not kill the watcher
            v = None
        if v:
            on_found(v)
            return v
        sleep(poll_every)
        waited += poll_every
    notify.send(f"⌛ TikTok {short_id}: no public post detected within 30 min. If you posted it, paste the URL manually.")
    return None


def start_watch(db, short_id, on_found) -> threading.Thread:
    t = threading.Thread(target=discover, args=(db, short_id, int(time.time()) - 60, on_found),
                         daemon=True, name=f"tiktok-watch-{short_id}")
    t.start()
    return t
