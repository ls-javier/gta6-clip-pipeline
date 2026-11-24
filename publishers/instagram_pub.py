"""Instagram Reels via Graph API v27.0, Facebook Login, 3-step resumable upload."""
import http.client
import json
import os
import time
import urllib.parse
from pathlib import Path

from db import tokens
from publishers._http import ApiError, http as _default_http

GRAPH = tokens.IG_GRAPH


def configured(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("IG_USER_ID") and env.get("IG_ACCESS_TOKEN"))


def stream_upload(uri, token, path, chunk=1 << 20, timeout=300):
    """POST the file to rupload.facebook.com in 1 MB chunks. (Sending a single multi-MB bytes body via urllib
    raised 'Broken pipe'.) If the server closes early we still read its response to surface the real error."""
    size = Path(path).stat().st_size
    u = urllib.parse.urlparse(uri)
    conn = http.client.HTTPSConnection(u.netloc, timeout=timeout)
    conn.putrequest("POST", u.path + ("?" + u.query if u.query else ""))
    for k, v in {"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
                 "Content-Length": str(size)}.items():
        conn.putheader(k, v)
    conn.endheaders()
    try:
        with open(path, "rb") as f:
            while block := f.read(chunk):
                conn.send(block)
    except (BrokenPipeError, ConnectionResetError):
        pass                                           # fall through: the response says why
    try:
        r = conn.getresponse()
        body = r.read()
    finally:
        conn.close()
    if r.status != 200:
        raise ApiError(f"Instagram upload HTTP {r.status}: {body[:300].decode('utf-8', 'replace')}")
    try:
        if json.loads(body).get("success") is False:
            raise ApiError(f"Instagram upload rejected: {body[:300].decode('utf-8', 'replace')}")
    except ValueError:
        pass


def upload(db, meta, dry, http=_default_http, token_http=tokens.default_http, env=None,
           sleep=time.sleep, poll_every=10, max_polls=60, streamer=None, attempts=4, retry_wait=15) -> dict:
    env = os.environ if env is None else env
    if dry:
        return {"success": False, "url": None, "error": "dry run: Instagram cannot post privately", "skipped": True}
    if env.get("IG_AUTH_MODE", "facebook_login") != "facebook_login":
        return {"success": False, "url": None,
                "error": "IG_AUTH_MODE=instagram_login cannot upload local files; use facebook_login"}
    token = tokens.get_valid_access_token(db, "instagram", env, token_http)
    uid = env["IG_USER_ID"]
    path = Path(meta["master_path"])
    # 1+2) create a resumable container and stream the master to rupload.facebook.com.
    # rupload can time out (HTTP 408 / broken pipe) on slow or jittery uplinks, so retry with a fresh container.
    last = None
    for attempt in range(1, attempts + 1):
        c = http("POST", f"{GRAPH}/{uid}/media", form={
            "media_type": "REELS", "upload_type": "resumable", "caption": meta["caption"][:2200],
            "access_token": token}, timeout=60)
        cid, uri = c["id"], c["uri"]
        try:
            (streamer or stream_upload)(uri, token, path)
            break
        except (ApiError, OSError) as e:
            last = e
            if attempt < attempts:
                sleep(retry_wait * attempt)
    else:
        raise ApiError(f"Instagram upload failed after {attempts} attempts: {last}")
    # 3) poll until FINISHED, then publish
    for _ in range(max_polls):
        st = http("GET", f"{GRAPH}/{cid}?" + urllib.parse.urlencode({"fields": "status_code,status", "access_token": token}),
                  timeout=30)
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise ApiError(f"Instagram container {st}")
        sleep(poll_every)
    else:
        raise ApiError("timed out waiting for Instagram container")
    m = http("POST", f"{GRAPH}/{uid}/media_publish", form={"creation_id": cid, "access_token": token}, timeout=60)
    media_id = m["id"]
    link = http("GET", f"{GRAPH}/{media_id}?" + urllib.parse.urlencode({"fields": "permalink", "access_token": token}),
                timeout=30).get("permalink")
    return {"success": True, "url": link, "video_id": media_id, "error": None}
