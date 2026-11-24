"""Phase 3 worker: queued clip -> (LLM gate) -> sanitize -> OpenShorts 9:16 -> awaiting_approval.
Strictly one clip at a time; Demucs and OpenShorts Whisper both run under heavy_lock()."""
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from watcher import audio_sanitizer, llm, openshorts_client as osc

log = logging.getLogger("worker")
CLIPS = Path(os.environ.get("CLIPS_DIR", "/data/clips"))
MIN_SCORE = int(os.environ.get("MIN_MOMENT_SCORE", "0"))   # 0 = score is logged, never blocks


def _trim(src: str, dst: str, start: float, end: float) -> str:
    """Cut `start` s off the beginning and `end` s off the end of the raw clip (re-encoded for exact cuts)."""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src],
                               capture_output=True, text=True, check=True).stdout)
    length = dur - start - end
    if length < 8:
        raise RuntimeError(f"trim leaves only {length:.1f}s")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-i", src, "-t", f"{length:.2f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", dst], check=True)
    return dst


def process(db, clip: dict):
    sid = clip["short_id"]
    chan = db.query("SELECT twitch_channel FROM streamers WHERE id=?", (clip["streamer_id"],))[0]["twitch_channel"]
    try:
        verdict = llm.score_moment(clip["chat_excerpt"] or "") if clip["chat_excerpt"] else None
        if verdict:
            log.info("clip %s score=%s", sid, verdict)
            if verdict.get("reject") or (MIN_SCORE and verdict.get("score", 100) < MIN_SCORE):
                db.set_status(sid, "skipped")
                log.info("clip %s skipped by moment gate", sid)
                return
        prm = json.loads(clip.get("recut_params") or "{}")
        hook = prm.get("hook_text") or llm.make_hook(clip["chat_excerpt"] or "")
        if hook:
            db.execute("UPDATE clips SET hook_text=? WHERE short_id=?", (hook, sid))

        tags = llm.pick_hashtags(clip["chat_excerpt"] or "", hook or "")
        db.execute("UPDATE clips SET hashtags=? WHERE short_id=?", (" ".join(tags), sid))

        db.set_status(sid, "sanitizing")
        san = str(CLIPS / chan / f"{sid}_clean.mp4")
        src = clip["raw_path"]
        if prm.get("trim_start") or prm.get("trim_end"):
            src = _trim(src, str(CLIPS / chan / f"{sid}_trim.mp4"), prm.get("trim_start", 0), prm.get("trim_end", 0))
        audio_sanitizer.sanitize(src, san, keep=audio_sanitizer.PRESETS[prm["audio"]] if prm.get("audio") else None)
        db.execute("UPDATE clips SET sanitized_path=? WHERE short_id=?", (san, sid))

        db.set_status(sid, "rendering")
        with audio_sanitizer.heavy_lock():          # keep Whisper off while nothing else heavy runs
            job = osc.submit(san, layouts=osc.layouts_for(clip.get("layout")),
                             clip_min=prm.get("clip_min", 15), clip_max=prm.get("clip_max", 60),
                             auto_hook=prm.get("auto_hook", True), captions=prm.get("captions", True))
            log.info("clip %s -> openshorts job %s", sid, job)
            result = osc.wait(job)
        master = osc.fetch_clip(result, job, str(CLIPS / chan / f"{sid}_916.mp4"))
        db.execute("UPDATE clips SET master_916_path=? WHERE short_id=?", (master, sid))
        db.set_status(sid, "awaiting_approval")
        log.info("clip %s ready: %s", sid, master)
        try:
            from watcher.telegram_bot import notify_n8n
            notify_n8n("clip-ready", sid)
        except Exception:
            log.exception("could not notify n8n for %s (clip stays awaiting_approval)", sid)
    except Exception:
        log.exception("clip %s failed", sid)
        db.set_status(sid, "failed")


def run_forever(db, interval=10):
    # A crash mid-clip leaves it in sanitizing/rendering: requeue on startup.
    db.execute("UPDATE clips SET status='queued' WHERE status IN ('sanitizing','rendering') AND raw_path IS NOT NULL")
    while True:
        rows = db.query("SELECT * FROM clips WHERE status='queued' AND raw_path IS NOT NULL ORDER BY created_at LIMIT 1")
        if rows:
            process(db, rows[0])
        else:
            time.sleep(interval)
