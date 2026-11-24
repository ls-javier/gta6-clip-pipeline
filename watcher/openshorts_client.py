"""Thin OpenShorts REST client (self-hosted, no key). Uses polling: OpenShorts only delivers
webhooks to public HTTPS URLs, which a local container is not. verify_signature() is provided
for when a public endpoint (e.g. a tunnel to n8n) is used."""
import hashlib
import hmac
import json
import os
import shutil
import time
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get("OPENSHORTS_URL", "http://openshorts-backend:8000")
OUTPUT_DIR = Path(os.environ.get("OPENSHORTS_OUTPUT", "/data/output"))


def verify_signature(raw_body: bytes, header: str, secret: str) -> bool:
    exp = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, header or "")


def _req(method, path, body=None, headers=None, timeout=60):
    r = urllib.request.Request(BASE + path, body, headers or {}, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.load(resp)


def _multipart(fields: dict, file_field: str, file_path: str):
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{Path(file_path).name}"\r\n'
                  f'Content-Type: video/mp4\r\n\r\n').encode())
    parts.append(Path(file_path).read_bytes())
    parts.append(f"\r\n--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


# Framings selectable from Telegram: key -> (button label, OpenShorts `layouts` value).
# Default is DEFAULT_FRAMING when a clip has no choice stored.
FRAMINGS = {
    "a": ("🖥 Juego horizontal + cara", "screencast"),   # full-width game on top, streamer face underneath
    "b": ("🙂 Cara + centro del juego", "split"),        # face band + game crop (verify on a real clip)
    "c": ("🎯 Auto (centro del juego)", "auto"),         # OpenShorts picks / face-tracking crop
}
DEFAULT_FRAMING = "a"


def layouts_for(key) -> str:
    return FRAMINGS.get(key or DEFAULT_FRAMING, FRAMINGS[DEFAULT_FRAMING])[1]


def submit(video_path: str, webhook_url=None, webhook_secret=None, layouts="screencast",
           clip_min=15, clip_max=60, auto_hook=True, captions=True) -> str:
    fields = {"acknowledged": "true", "output_format": "vertical", "layouts": layouts,
              "target_clips": "1", "clip_min_seconds": str(clip_min), "clip_max_seconds": str(clip_max),
              "auto_hook": "true" if auto_hook else "false", "captions": "true" if captions else "false"}
    if webhook_url:
        fields["webhook_url"] = webhook_url
        if webhook_secret:
            fields["webhook_secret"] = webhook_secret
    body, ctype = _multipart(fields, "file", video_path)
    j = _req("POST", "/api/process", body, {"Content-Type": ctype}, timeout=300)
    if "job_id" not in j:
        raise RuntimeError(f"openshorts refused job: {j}")
    return j["job_id"]


def wait(job_id: str, poll=20, timeout=3600) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = _req("GET", f"/api/status/{job_id}")
        if j.get("status") == "completed":
            return j["result"]
        if j.get("status") == "failed":
            raise RuntimeError(f"openshorts job failed: {(j.get('logs') or [''])[-3:]}")
        time.sleep(poll)
    raise TimeoutError(f"openshorts job {job_id} timed out")


def fetch_clip(result: dict, job_id: str, dst: str, index=0) -> str:
    """Copy the rendered clip out of the shared output volume (fallback: HTTP download)."""
    clip = result["clips"][index]
    name = Path((clip.get("video_url") or "").split("?")[0]).name
    src = OUTPUT_DIR / job_id / name
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copyfile(src, dst)
    else:
        url = clip.get("download_url") or (BASE + clip["video_url"])
        urllib.request.urlretrieve(url, dst)
    return dst
