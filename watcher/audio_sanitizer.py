"""Serialized Demucs stem scrubber + the shared heavy-job mutex.

Demucs and OpenShorts' Whisper must never overlap in 16 GB RAM, so both run while holding
heavy_lock() (a flock on a file in the shared /data volume).
"""
import contextlib
import fcntl
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("sanitizer")
LOCK_PATH = os.environ.get("HEAVY_LOCK", "/data/locks/heavy.lock")
# Which Demucs stems are mixed back in, and at what gain. Music is spread over every stem, so any stem
# kept besides `vocals` also keeps some music: that is the trade-off between a "empty" clip and copyright risk.
#   strict   : voice only (safest for licensed music; game SFX/ambience are lost)
#   balanced : voice + `other` (engine, ambience, crowd, gunfire tails) + `drums` (impacts, shots); bass is dropped
#              because it is mostly the music bed.
PRESETS = {
    "strict": {"vocals": 1.0},
    "balanced": {"vocals": 1.0, "other": 1.0, "drums": 1.0},
}
MODE = os.environ.get("SANITIZER_MODE", "balanced").lower()
if MODE not in PRESETS:
    raise ValueError(f"SANITIZER_MODE must be one of {sorted(PRESETS)}, got {MODE!r}")
MODEL = os.environ.get("DEMUCS_MODEL", "htdemucs")


@contextlib.contextmanager
def heavy_lock(timeout=None):
    Path(LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.strip()[-600:]}")


def separate(wav: str, outdir: str) -> dict:
    """Run Demucs on a wav; returns {stem: path}. Caller must hold heavy_lock()."""
    _run(["python", "-m", "demucs", "-n", MODEL, "-d", "cpu", "-j", "1", "--segment", "7",
          "-o", outdir, wav])
    d = Path(outdir) / MODEL / Path(wav).stem
    return {p.stem: str(p) for p in d.glob("*.wav")}


def sanitize(src_mp4: str, dst_mp4: str, keep=None) -> str:
    """keep: None (use SANITIZER_MODE), a list of stems (gain 1.0) or a {stem: gain} dict."""
    gains = PRESETS[MODE] if keep is None else (keep if isinstance(keep, dict) else {k: 1.0 for k in keep})
    keep = list(gains)
    with heavy_lock(), tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
        wav = str(Path(td) / "audio.wav")
        _run(["ffmpeg", "-v", "error", "-y", "-i", src_mp4, "-vn", "-ac", "2", "-ar", "44100", wav])
        stems = separate(wav, td)
        missing = [k for k in keep if k not in stems]
        if missing:
            raise RuntimeError(f"demucs did not produce stems {missing}; got {sorted(stems)}")
        inputs, filt = [], []
        for i, k in enumerate(keep):
            inputs += ["-i", stems[k]]
            filt.append(f"[{i+1}:a]volume={gains[k]}[s{i}]")
        mix = "".join(f"[s{i}]" for i in range(len(keep)))
        filt.append(f"{mix}amix=inputs={len(keep)}:normalize=0,alimiter=limit=0.95[a]")
        Path(dst_mp4).parent.mkdir(parents=True, exist_ok=True)
        _run(["ffmpeg", "-v", "error", "-y", "-i", src_mp4, *inputs, "-filter_complex", ";".join(filt),
              "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-movflags", "+faststart", dst_mp4])
    return dst_mp4
