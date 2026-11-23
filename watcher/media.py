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
