"""Unified publish entrypoint, called by n8n (via control_api /publish) after Telegram Approve.

Routing (Upload-Post vs native) lives in watcher/publishing.py. This layer adds the bounty bookkeeping:
a publications_v2 row per live post (campaign, submit deadline) and TikTok inbox discovery.
"""
import json
import logging
import time

from publishers import notify
from watcher import publishing

log = logging.getLogger("dispatcher")


def find_campaign(db, streamer_id, platform) -> dict | None:
    """Active campaign for this streamer (or an unlinked one) that allows `platform`; streamer-specific first."""
    rows = db.query("SELECT * FROM campaigns WHERE status='active' AND (streamer_id=? OR streamer_id IS NULL)"
                    " ORDER BY streamer_id IS NULL, id", (streamer_id,))
    for c in rows:
        if platform in json.loads(c["allowed_platforms"]):
            return c
    return None


def record_publication(db, short_id, platform, url, video_id=None, now=None) -> int:
    """Insert the publications_v2 row. submit_deadline_at = published_at + submission window."""
    now = int(now or time.time())
    sid = db.query("SELECT streamer_id FROM clips WHERE short_id=?", (short_id,))[0]["streamer_id"]
    camp = find_campaign(db, sid, platform)
    window = (camp["submission_window_min"] if camp else 30) * 60
    status = "pending_submission" if camp else "not_applicable"
    return db.execute(
        "INSERT INTO publications_v2 (clip_short_id,campaign_id,platform,platform_video_id,live_url,published_at,"
        "submit_deadline_at,bounty_status) VALUES (?,?,?,?,?,?,?,?)",
        (short_id, camp["id"] if camp else None, platform, video_id, url, now, now + window, status)).lastrowid


def _on_tiktok_found(db, short_id):
    def cb(video):
        url = video.get("share_url")
        db.execute("UPDATE publications SET live_url=? WHERE clip_short_id=? AND platform='tiktok'", (url, short_id))
        pid = record_publication(db, short_id, "tiktok", url, video["id"])
        row = db.query("SELECT submit_deadline_at FROM publications_v2 WHERE id=?", (pid,))[0]
        notify.send(f"🎵 TikTok publicado — clip {short_id}\n{url}\nPega el enlace en la campaña antes de "
                    f"{time.strftime('%H:%M', time.localtime(row['submit_deadline_at']))}.")
    return cb


def publish(db, short_id: str) -> dict:
    res = publishing.publish(db, short_id)
    if res["dry_run"]:                      # private test posts never enter bounty tracking
        return res
    from publishers import tiktok_inbox_pub
    for p, d in res.get("details", {}).items():
        if p == "tiktok" and d["via"] == "native" and not d["url"]:
            tiktok_inbox_pub.start_watch(db, short_id, _on_tiktok_found(db, short_id))
        elif d["url"]:
            record_publication(db, short_id, p, d["url"], d.get("video_id"))
    return res
