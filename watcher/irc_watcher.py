"""Anonymous Twitch IRC client + sliding-window spike detector (pure Python, no LLM)."""
import asyncio
import collections
import logging
import os
import re
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

# Laughter trigger: something funny happened even if total chat volume did not reach RATIO x baseline.
LAUGH_ENABLED = os.environ.get("LAUGH_TRIGGER", "1") != "0"
LAUGH_MIN_MSGS = int(os.environ.get("LAUGH_MIN_MSGS", "6"))               # laughing messages in one 10 s bin
LAUGH_MIN_FRACTION = float(os.environ.get("LAUGH_MIN_FRACTION", "0.3"))   # ...and at least this share of the bin
LAUGH_RE = re.compile(
    r"\b[aeiou]?(?:[jh][aeiou]){3,}[jh]?\b|\b[aeiou]?([jh][aeiou])\1[jh]?\b"       # jajaja, ajajaj, jejeje, hahaha, jaja, hihi
    r"|\bk{4,}\b|\bxd+\b"                                                       # kkkk, xd, xddd
    r"|\b(?:lol|lmao|lmfao|rofl|lul|lulw|kekw|kekl|omegalul|icant|pepelaugh|jajaja)\b"
    r"|[😂🤣😆😹💀]", re.I)


def is_laugh(text: str) -> bool:
    return bool(LAUGH_RE.search(text))


class SpikeDetector:
    """Feed message timestamps via add(); returns (ratio, count, baseline) on a trigger."""

    def __init__(self, clock: Callable[[], float] = time.time, ratio=RATIO, cooldown=COOLDOWN,
                 min_count=MIN_COUNT, min_history=MIN_HISTORY_BINS):
        self.clock, self.ratio, self.cooldown = clock, ratio, cooldown
        self.min_count, self.min_history = min_count, min_history
        self.history: collections.deque[int] = collections.deque(maxlen=BASELINE_BINS)
        self.bin_start: Optional[float] = None
        self.count = 0
        self.laugh = 0
        self.last_reason = "ratio"
        self.last_trigger = float("-inf")

    def _roll(self, now: float):
        if self.bin_start is None:
            self.bin_start = now
            return
        while now - self.bin_start >= BIN_SECONDS:
            self.history.append(self.count)
            self.count = 0
            self.laugh = 0
            self.bin_start += BIN_SECONDS

    def baseline(self) -> float:
        return sum(self.history) / len(self.history) if self.history else 0.0

    def tick(self, now: Optional[float] = None):
        self._roll(self.clock() if now is None else now)

    def add(self, now: Optional[float] = None, text: Optional[str] = None):
        now = self.clock() if now is None else now
        self._roll(now)
        self.count += 1
        if text is not None and LAUGH_ENABLED and is_laugh(text):
            self.laugh += 1
        cooled = now - self.last_trigger >= self.cooldown
        base = max(self.baseline(), MIN_BASELINE)
        if len(self.history) >= self.min_history and self.count >= self.min_count \
                and self.count >= self.ratio * base and cooled:
            self.last_trigger, self.last_reason = now, "ratio"
            return (self.count / base, self.count, base)
        # Laughter bypasses the volume ratio: many "jajaja"/LUL/😂 in one bin = something funny happened.
        if LAUGH_ENABLED and len(self.history) >= 3 and cooled and self.laugh >= LAUGH_MIN_MSGS \
                and self.laugh >= LAUGH_MIN_FRACTION * self.count:
            self.last_trigger, self.last_reason = now, "laugh"
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
        hit = self.det.add(now, m[1])
        if hit:
            ratio, count, base = hit
            excerpt = self.excerpt(now)
            if self.det.last_reason == "laugh":
                excerpt = f"[El chat se ríe: {self.det.laugh} de {count} mensajes en 10 s]\n" + excerpt
                log.info("LAUGH #%s %d/%d msgs in 10s (ratio %.1fx vs baseline %.1f)", self.channel, self.det.laugh, count, ratio, base)
            else:
                log.info("SPIKE #%s ratio=%.1fx (%d msgs/10s vs baseline %.1f)", self.channel, ratio, count, base)
            self.on_spike(now, ratio, excerpt)
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
