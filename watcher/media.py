"""480p Telegram proxy via Intel VA-API (falls back to libx264 if VA-API fails)."""
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("media")
VAAPI_DEV = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
MAX_BYTES = 14 * 1024 * 1024


def make_preview(src: str, dst: str) -> str:
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    for kbps in (1200, 800, 500):
        vaapi = ["-init_hw_device", f"vaapi=va:{VAAPI_DEV}", "-filter_hw_device", "va", "-i", src,
                 "-vf", "scale=480:854:force_original_aspect_ratio=decrease,pad=480:854:(ow-iw)/2:(oh-ih)/2,format=nv12,hwupload",
                 "-c:v", "h264_vaapi", "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps*1.2)}k"]
        sw = ["-i", src, "-vf", "scale=480:854:force_original_aspect_ratio=decrease,pad=480:854:(ow-iw)/2:(oh-ih)/2",
              "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps*1.2)}k", "-bufsize", f"{kbps*2}k"]
        tail = ["-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst]
        for enc in (vaapi, sw):
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", *enc, *tail], capture_output=True, text=True)
            if r.returncode == 0:
                break
            log.warning("preview encode failed (%s): %s", "vaapi" if enc is vaapi else "sw", r.stderr.strip()[-300:])
        else:
            raise RuntimeError("preview encode failed")
        if os.path.getsize(dst) <= MAX_BYTES:
            return dst
    raise RuntimeError(f"preview still >{MAX_BYTES} bytes")


def first_frame(src: str, dst: str, max_bytes: int = 1_900_000) -> str:
    """JPEG of the very first video frame (the hook pill is burned in from t=0), kept under YouTube's 2 MB limit."""
    for q in (2, 4, 7, 12):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-frames:v", "1",
                        "-q:v", str(q), dst], check=True, timeout=60)
        if os.path.getsize(dst) <= max_bytes:
            return dst
    raise RuntimeError("first-frame thumbnail exceeds size limit")
