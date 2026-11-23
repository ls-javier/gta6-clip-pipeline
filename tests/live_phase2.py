"""Live 10-min run: python -m tests.live_phase2 <channel> [minutes]. Mock burst injected at T-90s."""
import asyncio, sys, time, logging, os
from pathlib import Path
from db.state import State
from watcher.buffer_manager import BufferManager
from watcher.irc_watcher import IrcWatcher
from watcher.main import make_spike_handler
import watcher.main as m

chan = sys.argv[1]; minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 10
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
m.CLIPS_DIR = Path("/data/clips")
async def main():
    db = State("/data/pipeline_live_test.db")
    sid = db.add_streamer(chan, "@" + chan) if not db.query("select 1 from streamers") else 1
    buf = BufferManager(chan, "/data/buffer"); buf.start()
    irc = IrcWatcher(chan, make_spike_handler(db, sid, chan, buf))
    t = asyncio.create_task(irc.run())
    t0 = time.time(); injected = False; real = 0
    while time.time() - t0 < minutes * 60:
        await asyncio.sleep(30)
        logging.info("T+%3.0fs disk=%.1f MB segs=%d clips=%s real_spikes=%d baseline=%.1f/10s", time.time()-t0,
                     buf.disk_usage()/1e6, len(list(buf.dir.glob('seg_*.ts'))),
                     [(c['short_id'], c['status']) for c in db.query('select short_id,status from clips')], real, irc.det.baseline())
        if not injected and time.time() - t0 > minutes * 60 - 100:
            injected = True
            logging.info("INJECTING mock 3x+ burst")
            for i in range(int(irc.det.baseline() * 5) + 20):
                irc.handle_line(f":mock{i}!m@m.tmi.twitch.tv PRIVMSG #{chan} :MOCK BURST {i}")
    await asyncio.sleep(25)
    for c in db.query("select * from clips"): logging.info("CLIP %s", c)
    irc.stop(); t.cancel(); buf.stop()
asyncio.run(main())
