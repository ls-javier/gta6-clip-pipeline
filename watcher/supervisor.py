"""Starts/stops per-streamer capture. 'always' streamers run whenever the daemon runs; 'gta6_only'
streamers run only while live AND playing GTA VI, and at most MAX_GTA6_GROUP (default 2, highest
viewers) at a time. Running ones are kept while still eligible (no flapping)."""
import asyncio
import logging
import os
import shutil
from pathlib import Path

from watcher import twitch_api
from watcher.buffer_manager import BufferManager
from watcher.irc_watcher import IrcWatcher, SpikeDetector, RATIO

log = logging.getLogger("supervisor")
POLL = int(os.environ.get("GROUP_POLL_SECONDS", "90"))
MAX_GROUP = int(os.environ.get("MAX_GTA6_GROUP", "2"))


class Supervisor:
    def __init__(self, db, buffer_dir, handler_factory):
        self.db, self.buffer_dir, self.handler_factory = db, buffer_dir, handler_factory
        self.running: dict[str, tuple[BufferManager, asyncio.Task]] = {}

    def start_one(self, s: dict):
        ch = s["twitch_channel"]
        if ch in self.running:
            return
        buf = BufferManager(ch, self.buffer_dir)
        buf.start()
        ratio = s.get("spike_ratio_threshold") or RATIO
        irc = IrcWatcher(ch, self.handler_factory(self.db, s["id"], ch, buf), detector=SpikeDetector(ratio=ratio))
        self.running[ch] = (buf, asyncio.create_task(irc.run()))
        log.info("TRACKING %s (spike ratio %.2fx)", ch, ratio)

    def stop_one(self, ch: str):
        buf, task = self.running.pop(ch)
        task.cancel()
        buf.stop()
        shutil.rmtree(Path(self.buffer_dir) / ch, ignore_errors=True)
        log.info("STOPPED tracking %s", ch)

    def select_group(self, group: list, live: dict) -> list:
        """Pick which gta6_only channels should run, given {login: {'viewers','game'}}."""
        eligible = {s["twitch_channel"]: live[s["twitch_channel"]]["viewers"] for s in group
                    if s["twitch_channel"] in live and twitch_api.is_gta6(live[s["twitch_channel"]]["game"])}
        keep = [c for c in self.running if c in eligible and any(s["twitch_channel"] == c for s in group)]
        keep = sorted(keep, key=lambda c: -eligible[c])[:MAX_GROUP]
        free = [c for c in sorted(eligible, key=lambda c: -eligible[c]) if c not in keep]
        return keep + free[:max(0, MAX_GROUP - len(keep))]

    async def run(self):
        while True:
            rows = self.db.query("SELECT * FROM streamers WHERE enabled=1")
            always = [s for s in rows if s["mode"] == "always"]
            group = [s for s in rows if s["mode"] == "gta6_only"]
            for s in always:
                self.start_one(s)
            for ch in [c for c in self.running if c not in {s["twitch_channel"] for s in rows}]:
                self.stop_one(ch)                                 # disabled/removed in the DB
            if group:
                if not twitch_api.configured():
                    log.warning("gta6_only streamers exist but TWITCH_CLIENT_ID/SECRET are not set: not tracking them")
                else:
                    try:
                        live = await asyncio.to_thread(twitch_api.live_streams, [s["twitch_channel"] for s in group])
                        want = self.select_group(group, live)
                        log.info("gta6_only live+GTA6: %s -> tracking %s",
                                 {c: v["viewers"] for c, v in live.items() if twitch_api.is_gta6(v["game"])}, want)
                        gnames = {s["twitch_channel"] for s in group}
                        for ch in [c for c in self.running if c in gnames and c not in want]:
                            self.stop_one(ch)
                        for s in group:
                            if s["twitch_channel"] in want:
                                self.start_one(s)
                    except Exception as e:
                        log.warning("group poll failed (keeping current set): %s", e)
            await asyncio.sleep(POLL)
