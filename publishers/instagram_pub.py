"""Instagram Reels via Graph API v27.0, Facebook Login, 3-step resumable upload."""
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


def upload(db, meta, dry, http=_default_http, token_http=tokens.default_http, env=None,
           sleep=time.sleep, poll_every=10, max_polls=60) -> dict:
    env = os.environ if env is None else env
    if dry:
        return {"success": False, "url": None, "error": "dry run: Instagram cannot post privately", "skipped": True}
    if env.get("IG_AUTH_MODE", "facebook_login") != "facebook_login":
        return {"success": False, "url": None,
                "error": "IG_AUTH_MODE=instagram_login cannot upload local files; use facebook_login"}
    token = tokens.get_valid_access_token(db, "instagram", env, token_http)
    uid = env["IG_USER_ID"]
    path = Path(meta["master_path"])
    size = path.stat().st_size
    # 1) create resumable container
    c = http("POST", f"{GRAPH}/{uid}/media", form={
        "media_type": "REELS", "upload_type": "resumable", "caption": meta["caption"][:2200], "access_token": token},
        timeout=60)
    cid, uri = c["id"], c["uri"]
    # 2) stream the master to rupload.facebook.com
    http("POST", uri, data=path.read_bytes(), headers={"Authorization": f"OAuth {token}", "offset": "0",
                                                       "file_size": str(size)})
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
