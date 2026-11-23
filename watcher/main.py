"""Phase 2 daemon: per enabled streamer -> ring buffer + IRC watcher; spikes -> slices -> SQLite.

  python -m watcher.main add <channel> <@credit_handle> [campaign]
  python -m watcher.main run
"""
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

from db.state import State
from watcher.buffer_manager import BufferManager
from watcher.irc_watcher import IrcWatcher
from watcher.supervisor import Supervisor
from watcher import pipeline_worker, telegram_bot, control_api

DB_PATH = os.environ.get("DB_PATH", "/data/pipeline.db")
BUFFER_DIR = os.environ.get("BUFFER_DIR", "/data/buffer")
CLIPS_DIR = Path(os.environ.get("CLIPS_DIR", "/data/clips"))
BEFORE, AFTER = 30, 15
log = logging.getLogger("main")


def make_spike_handler(db: State, streamer_id: int, channel: str, buf: BufferManager):
    def handle(spike_ts: float, ratio: float, excerpt: str):
        def work():
            import time
            time.sleep(AFTER + 4)                      # let post-spike footage land on disk
            sid = db.new_clip(streamer_id, spike_ts, ratio, excerpt)
            out = CLIPS_DIR / channel / f"{sid}_raw.mp4"
            try:
                info = buf.slice(spike_ts, str(out), BEFORE, AFTER)
                db.execute("UPDATE clips SET raw_path=? WHERE short_id=?", (str(out), sid))
                log.info("clip %s logged: %s (%.1fs)", sid, out, info["duration"])
            except Exception as e:
                db.set_status(sid, "failed")
                log.error("clip %s failed: %s", sid, e)
        threading.Thread(target=work, daemon=True).start()
    return handle


async def run():
    db = State(DB_PATH)
    # A restart mid-slice leaves a clip row with no file; it can never be processed.
    db.execute("UPDATE clips SET status='failed' WHERE status='queued' AND raw_path IS NULL")
    control_api.make_server(db)
    threading.Thread(target=telegram_bot.poll_forever, args=(db,), daemon=True, name="telegram").start()
    threading.Thread(target=pipeline_worker.run_forever, args=(db,), daemon=True, name="worker").start()
    await Supervisor(db, BUFFER_DIR, make_spike_handler).run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "add":
        db = State(DB_PATH)
        print(db.add_streamer(sys.argv[2].lower(), sys.argv[3], campaign_name=(sys.argv[4] if len(sys.argv) > 4 else None)))
    else:
        asyncio.run(run())
