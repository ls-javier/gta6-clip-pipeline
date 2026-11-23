"""YouTube Shorts via Data API v3 resumable videos.insert (100 uploads/day bucket)."""
import os
from pathlib import Path

from db import tokens
from publishers._http import ApiError, http as _default_http

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status,paidProductPlacementDetails"
AUDIT_URL = "https://developers.google.com/youtube/data/guides/quota_and_compliance_audits"


def configured(env=None) -> bool:
    env = os.environ if env is None else env
    return all(env.get(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"))


def build_body(meta, dry) -> dict:
    """Since 2026-11-16 hasPaidProductPlacement must be sent explicitly (no longer defaults to false)."""
    return {"snippet": {"title": meta["yt_title"][:100], "description": meta["yt_description"][:5000], "categoryId": "20"},
            "status": {"privacyStatus": "private" if dry else "public", "selfDeclaredMadeForKids": False},
            "paidProductPlacementDetails": {"hasPaidProductPlacement": False}}


def upload(db, meta, dry, http=_default_http, token_http=tokens.default_http, env=None) -> dict:
    paused = tokens.youtube_paused_until(db)
    if paused:                                       # circuit breaker: failed retries still cost quota
        return {"success": False, "url": None, "error": f"YouTube paused (quota) until unix {paused}"}
    path = Path(meta["master_path"])
    size = path.stat().st_size
    access = tokens.get_valid_access_token(db, "youtube", env, token_http)
    try:
        _, hdrs = http("POST", UPLOAD_URL, data=build_body(meta, dry), want_headers=True, timeout=60, headers={
            "Authorization": f"Bearer {access}", "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4"})
        loc = next((v for k, v in hdrs.items() if k.lower() == "location"), None)
        if not loc:
            raise ApiError("YouTube did not return a resumable upload URL")
        res = http("PUT", loc, data=path.read_bytes(), headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
    except ApiError as e:
        if "quotaExceeded" in str(e):
            tokens.trip_youtube_breaker(db)
            return {"success": False, "url": None, "error": "YouTube quotaExceeded: paused until next daily window"}
        raise
    vid = res.get("id")
    if not vid:
        raise ApiError(f"YouTube upload returned no id: {res}")
    actual = (res.get("status") or {}).get("privacyStatus")
    note = None
    if not dry and actual and actual != "public":    # unaudited API projects get locked to private
        note = f"YouTube locked the upload to '{actual}' (unaudited project). Compliance Audit form: {AUDIT_URL}"
    return {"success": True, "url": f"https://youtube.com/shorts/{vid}", "video_id": vid, "error": None, "note": note}
