"""Anonymous Twitch IRC client + sliding-window spike detector (pure Python, no LLM)."""
import asyncio
import collections
import logging
import os
import ssl
import time
from typing import Callable, Optional

log = logging.getLogger("irc")

BIN_SECONDS = 10
BASELINE_BINS = 30          # 5 minutes
MIN_HISTORY_BINS = 6        # need >= 1 min of history before triggering
RATIO = float(os.environ.get("SPIKE_RATIO", "3.0"))
COOLDOWN = float(os.environ.get("SPIKE_COOLDOWN", "90"))
MIN_COUNT = int(os.environ.get("SPIKE_MIN_COUNT", "10"))             # absolute floor so a dead chat going 1->3 msgs isn't a "spike"
MIN_BASELINE = 1.0          # msgs per bin floor for the ratio


class SpikeDetector:
    """Feed message timestamps via add(); returns (ratio, count, baseline) on a trigger."""

    def __init__(self, clock: Callable[[], float] = time.time, ratio=RATIO, cooldown=COOLDOWN,
                 min_count=MIN_COUNT, min_history=MIN_HISTORY_BINS):
        self.clock, self.ratio, self.cooldown = clock, ratio, cooldown
        self.min_count, self.min_history = min_count, min_history
        self.history: collections.deque[int] = collections.deque(maxlen=BASELINE_BINS)
        self.bin_start: Optional[float] = None
        self.count = 0
        self.last_trigger = float("-inf")

    def _roll(self, now: float):
        if self.bin_start is None:
            self.bin_start = now
            return
        while now - self.bin_start >= BIN_SECONDS:
            self.history.append(self.count)
            self.count = 0
            self.bin_start += BIN_SECONDS

    def baseline(self) -> float:
        return sum(self.history) / len(self.history) if self.history else 0.0

    def tick(self, now: Optional[float] = None):
        self._roll(self.clock() if now is None else now)

    def add(self, now: Optional[float] = None):
        now = self.clock() if now is None else now
        self._roll(now)
        self.count += 1
        if len(self.history) < self.min_history or self.count < self.min_count:
            return None
        base = max(self.baseline(), MIN_BASELINE)
        if self.count >= self.ratio * base and now - self.last_trigger >= self.cooldown:
            self.last_trigger = now
            return (self.count / base, self.count, base)
        return None


def parse_privmsg(line: str):
    """':nick!nick@nick.tmi.twitch.tv PRIVMSG #chan :text' -> (nick, text) or None."""
    if " PRIVMSG " not in line:
        return None
    if line.startswith("@"):                       # strip IRCv3 tags
        line = line.split(" ", 1)[1]
    try:
        prefix, rest = line[1:].split(" PRIVMSG ", 1)
        text = rest.split(" :", 1)[1]
    except (ValueError, IndexError):
        return None
    return prefix.split("!", 1)[0], text


class IrcWatcher:
    def __init__(self, channel: str, on_spike: Callable[[float, float, str], None],
                 host="irc.chat.twitch.tv", port=int(os.environ.get("TWITCH_IRC_PORT", "6697")), detector: Optional[SpikeDetector] = None):
        self.channel = channel.lower().lstrip("#")
        self.on_spike = on_spike
        self.host, self.port = host, port
        self.det = detector or SpikeDetector()
        self.recent: collections.deque = collections.deque()   # (ts, nick, text)
        self._stop = False

    def excerpt(self, now: float, seconds=45) -> str:
        return "\n".join(f"{n}: {t}" for ts, n, t in self.recent if now - ts <= seconds)

    def handle_line(self, line: str, now: Optional[float] = None) -> Optional[str]:
        """Process one IRC line. Returns a PONG reply when needed."""
        now = time.time() if now is None else now
        if line.startswith("PING"):
            return "PONG" + line[4:]
        m = parse_privmsg(line)
        if not m:
            return None
        self.recent.append((now, *m))
        while self.recent and now - self.recent[0][0] > 60:
            self.recent.popleft()
        hit = self.det.add(now)
        if hit:
            ratio, count, base = hit
            log.info("SPIKE #%s ratio=%.1fx (%d msgs/10s vs baseline %.1f)", self.channel, ratio, count, base)
            self.on_spike(now, ratio, self.excerpt(now))
        return None

    async def run(self):
        backoff = 2
        while not self._stop:
            try:
                reader, writer = await asyncio.open_connection(
                    self.host, self.port, ssl=ssl.create_default_context() if self.port == 6697 else None)
                for l in ("NICK justinfan1234", f"JOIN #{self.channel}"):
                    writer.write((l + "\r\n").encode())
                await writer.drain()
                backoff = 2
                while True:
                    # readline timeout doubles as the detector clock tick for quiet chats
                    try:
                        raw = await asyncio.wait_for(reader.readline(), timeout=BIN_SECONDS)
                    except asyncio.TimeoutError:
                        self.det.tick()
                        continue
                    if not raw:
                        break
                    for line in raw.decode("utf-8", "replace").split("\r\n"):
                        if not line:
                            continue
                        reply = self.handle_line(line)
                        if reply:
                            writer.write((reply + "\r\n").encode())
                            await writer.drain()
                writer.close()
            except (OSError, asyncio.IncompleteReadError) as e:
                log.warning("irc #%s error: %s", self.channel, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def stop(self):
        self._stop = True
