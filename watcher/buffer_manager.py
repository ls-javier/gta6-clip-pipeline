"""Streamlink -> FFmpeg 30x60s ring buffer, plus safe slicing.

Segments are written as seg_00..seg_29.ts and overwritten (segment_wrap).
Wall-clock placement of a segment: end = file mtime, start = end - ffprobe duration.
This holds for the still-growing newest segment too, so slices can include
footage up to ~now.
"""
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("buffer")

SEG_SECONDS = 60
SEG_COUNT = 30


def probe(path) -> dict:
    """Return {'duration': float, 'start_time': float, 'has_video': bool, 'has_audio': bool}."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,start_time:stream=codec_type",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {r.stderr.strip()}")
    j = json.loads(r.stdout)
    fmt = j.get("format", {})
    kinds = {s.get("codec_type") for s in j.get("streams", [])}
    return {"duration": float(fmt.get("duration") or 0), "start_time": float(fmt.get("start_time") or 0),
            "has_video": "video" in kinds, "has_audio": "audio" in kinds}


class BufferManager:
    def __init__(self, channel: str, buffer_root: str, quality: str = "best",
                 codecs: str = "h264,h265,av1"):
        self.channel = channel.lower().lstrip("#")
        self.dir = Path(buffer_root) / self.channel
        self.quality = quality
        self.codecs = codecs
        self._stop = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._thread: threading.Thread | None = None

    # ---- capture -------------------------------------------------------
    def _cmd(self):
        # NOTE: no --twitch-disable-ads: deprecated/removed in Streamlink 8.x.
        sl = ["streamlink", f"twitch.tv/{self.channel}", self.quality, "--retry-streams", "30",
              "--twitch-supported-codecs", self.codecs, "-O", "--loglevel", "warning"]
        ff = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", "pipe:0", "-c", "copy",
              "-f", "segment", "-segment_time", str(SEG_SECONDS), "-segment_wrap", str(SEG_COUNT),
              "-reset_timestamps", "1", str(self.dir / "seg_%02d.ts")]
        return sl, ff

    def _run_once(self):
        sl_cmd, ff_cmd = self._cmd()
        sl = subprocess.Popen(sl_cmd, stdout=subprocess.PIPE)
        ff = subprocess.Popen(ff_cmd, stdin=sl.stdout)
        sl.stdout.close()
        self._procs = [sl, ff]
        while not self._stop.is_set() and ff.poll() is None:
            time.sleep(1)
        for p in (sl, ff):
            if p.poll() is None:
                p.terminate()
        for p in (sl, ff):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception:
                log.exception("capture crashed")
            if not self._stop.is_set():
                log.warning("capture for %s ended; restarting in 15s", self.channel)
                self._stop.wait(15)

    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name=f"buf-{self.channel}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        if self._thread:
            self._thread.join(timeout=20)

    # ---- inspection ----------------------------------------------------
    def disk_usage(self) -> int:
        return sum(f.stat().st_size for f in self.dir.glob("seg_*.ts"))

    def segments(self):
        """[(start_wall, end_wall, path)] sorted by start; skips unprobeable files."""
        out = []
        for f in self.dir.glob("seg_*.ts"):
            try:
                end = f.stat().st_mtime
                dur = probe(f)["duration"]
            except Exception:
                continue
            if dur <= 0:
                continue
            out.append((end - dur, end, f))
        return sorted(out)

    # ---- slicing -------------------------------------------------------
    def slice(self, spike_ts: float, out_path: str, before: float = 30, after: float = 15) -> dict:
        """Cut [spike-before, spike+after] to a verified MP4. Raises on failure."""
        w_start, w_end = spike_ts - before, spike_ts + after
        segs = [s for s in self.segments() if s[1] > w_start and s[0] < w_end]
        if not segs:
            raise RuntimeError("no buffered segments cover the requested window")
        first_start = segs[0][0]
        offset = max(0.0, w_start - first_start)
        length = min(w_end, segs[-1][1]) - max(w_start, first_start)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            lst = Path(td) / "list.txt"
            lst.write_text("".join(f"file '{p}'\n" for _, _, p in segs))
            # Re-encode (not -c copy) for frame-accurate cut and clean timestamps.
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-fflags", "+genpts", "-i", str(lst),
                   "-ss", f"{offset:.3f}", "-t", f"{length:.3f}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                   "-avoid_negative_ts", "make_zero", str(out_path)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg slice failed: {r.stderr.strip()[-500:]}")
        info = probe(out_path)
        if not info["has_video"] or info["duration"] < min(length, before + after) - 3:
            raise RuntimeError(f"slice failed verification: {info} (wanted ~{length:.1f}s)")
        info["requested"] = length
        return info
