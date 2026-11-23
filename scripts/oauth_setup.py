#!/usr/bin/env python3
"""One-time OAuth setup for the native publishers. Writes tokens into .env (and pipeline.db if it exists).

  python3 scripts/oauth_setup.py youtube      # needs YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET in .env
  python3 scripts/oauth_setup.py tiktok       # needs TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REDIRECT_URI
  python3 scripts/oauth_setup.py instagram    # needs IG_APP_ID / IG_APP_SECRET + a short-lived token from Graph API Explorer
  python3 scripts/oauth_setup.py check        # shows which values are set (never prints secrets)

Secrets are read from .env / hidden prompts and never printed.
"""
import argparse
import getpass
import http.server as hs
import json
import os
import re
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from publishers._http import ApiError, http as _http  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GRAPH = "https://graph.facebook.com/v27.0"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
TIKTOK_SCOPES = "user.info.basic,video.upload,video.list"


# ------------------------------------------------------------------ .env I/O
def read_env(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        m = re.match(r"\s*([A-Z][A-Z0-9_]*)=(.*)", line)
        if m:
            out[m[1]] = m[2].strip().strip('"').strip("'")
    return out


def update_env(path: Path, values: dict):
    """Set KEY=VALUE lines in place (append if absent). Keeps every other line untouched."""
    lines = path.read_text().splitlines() if path.exists() else []
    pending = dict(values)
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z][A-Z0-9_]*)=", line)
        if m and m[1] in pending:
            lines[i] = f"{m[1]}={pending.pop(m[1])}"
    if pending:
        lines += ["", "# --- written by scripts/oauth_setup.py ---"] + [f"{k}={v}" for k, v in pending.items()]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def save_to_db(db_path: Path, platform, access, refresh, expires_in, extra=None):
    """Best-effort copy into pipeline.db. The DB is often root-owned (created by the container), in which case
    we skip: .env is already updated and the pipeline seeds the DB from it on first use."""
    if not db_path.exists():
        return False
    import sqlite3
    from db import tokens
    from db.state import State
    try:
        db = State(str(db_path))
        try:
            tokens.save_token(db, platform, access, refresh, expires_in=expires_in, extra=extra)
        finally:
            db.close()
        return True
    except sqlite3.OperationalError as e:
        print(f"   (note: could not write {db_path}: {e}. Tokens are in .env; the pipeline seeds the DB from it "
              f"on first use. If the DB already has an old {platform} row, delete it: "
              f"docker compose exec watcher sqlite3 /data/pipeline.db \"DELETE FROM oauth_tokens WHERE platform='{platform}'\")")
        return False


def need(env, *keys):
    missing = [k for k in keys if not env.get(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}. Fill them in first (see .env.example).")


def parse_code(text: str) -> str:
    """Accepts a bare code or the full redirected URL; returns the decoded authorization code."""
    text = text.strip()
    if "code=" in text:
        q = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        vals = urllib.parse.parse_qs(q).get("code")
        if not vals:
            raise ValueError("no code= in the pasted text")
        return vals[0]
    return urllib.parse.unquote(text)


# ------------------------------------------------------------------- YouTube
def youtube_flow(env, env_path, db_path, http=_http, open_browser=True):
    need(env, "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET")
    state = secrets.token_urlsafe(16)
    box = {}

    class H(hs.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            box["code"], box["state"], box["error"] = (q.get("code", [None])[0], q.get("state", [None])[0],
                                                       q.get("error", [None])[0])
            self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab and return to the terminal.")

    srv = hs.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": env["YOUTUBE_CLIENT_ID"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(YT_SCOPES), "access_type": "offline", "prompt": "consent", "state": state})
    print("\nOpen this link, log in with the Google account that owns the YouTube channel and approve.")
    print("(If Google says the app is unverified: Advanced -> Go to app (unsafe) -> Continue.)\n")
    print(url, "\n")
    if open_browser:
        webbrowser.open(url)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start(); t.join(300)
    srv.server_close()
    if box.get("error") or not box.get("code"):
        sys.exit(f"Authorization failed or timed out: {box.get('error')}")
    if box.get("state") != state:
        sys.exit("State mismatch; aborting.")
    j = http("POST", "https://oauth2.googleapis.com/token", form={
        "code": box["code"], "client_id": env["YOUTUBE_CLIENT_ID"], "client_secret": env["YOUTUBE_CLIENT_SECRET"],
        "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    if not j.get("refresh_token"):
        sys.exit("Google returned no refresh token. Revoke the app at myaccount.google.com/permissions and retry.")
    ch = http("GET", "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
              headers={"Authorization": f"Bearer {j['access_token']}"}, timeout=30)
    name = ((ch.get("items") or [{}])[0].get("snippet") or {}).get("title", "(no channel found!)")
    update_env(env_path, {"YOUTUBE_REFRESH_TOKEN": j["refresh_token"]})
    save_to_db(db_path, "youtube", j["access_token"], j["refresh_token"], j.get("expires_in", 3600))
    print(f"✅ YouTube connected: channel '{name}'. YOUTUBE_REFRESH_TOKEN saved to .env")
    print("   Reminder: the OAuth app must be 'In production' or this token dies after 7 days.")


# -------------------------------------------------------------------- TikTok
def tiktok_auth_url(env, state):
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": env["TIKTOK_CLIENT_KEY"], "scope": TIKTOK_SCOPES, "response_type": "code",
        "redirect_uri": env["TIKTOK_REDIRECT_URI"], "state": state})


def tiktok_flow(env, env_path, db_path, http=_http, ask=input, open_browser=True):
    need(env, "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REDIRECT_URI")
    state = secrets.token_urlsafe(12)
    url = tiktok_auth_url(env, state)
    print("\nOpen this link while logged into the TikTok account listed as a Sandbox target user and approve.")
    print("You'll land on your redirect page; copy the FULL address from the browser bar.\n")
    print(url, "\n")
    if open_browser:
        webbrowser.open(url)
    code = parse_code(ask("Paste the redirected URL (or the code): "))
    j = http("POST", "https://open.tiktokapis.com/v2/oauth/token/", form={
        "client_key": env["TIKTOK_CLIENT_KEY"], "client_secret": env["TIKTOK_CLIENT_SECRET"], "code": code,
        "grant_type": "authorization_code", "redirect_uri": env["TIKTOK_REDIRECT_URI"]}, timeout=30)
    if "access_token" not in j:
        sys.exit(f"TikTok refused the code: {json.dumps({k: v for k, v in j.items() if 'token' not in k})}")
    update_env(env_path, {"TIKTOK_ACCESS_TOKEN": j["access_token"], "TIKTOK_REFRESH_TOKEN": j["refresh_token"],
                          "TIKTOK_OPEN_ID": j["open_id"]})
    save_to_db(db_path, "tiktok", j["access_token"], j["refresh_token"], j.get("expires_in", 86400),
               extra={"open_id": j["open_id"]})
    print(f"✅ TikTok connected (scopes: {j.get('scope')}). Tokens saved to .env")
    if "video.publish" in (j.get("scope") or ""):
        print("   ⚠ video.publish is granted: posts would be forced private in Sandbox. Remove that scope in the portal.")


# ----------------------------------------------------------------- Instagram
def instagram_flow(env, env_path, db_path, http=_http, ask=input, ask_secret=getpass.getpass):
    need(env, "IG_APP_ID", "IG_APP_SECRET")
    print("\nIn Graph API Explorer (developers.facebook.com/tools/explorer): pick your app, 'Get User Access Token' with")
    print("instagram_basic, instagram_content_publish, instagram_manage_insights, pages_show_list, pages_read_engagement.")
    short = ask_secret("Paste the short-lived token (hidden): ").strip()
    j = http("GET", f"{GRAPH}/oauth/access_token?" + urllib.parse.urlencode({
        "grant_type": "fb_exchange_token", "client_id": env["IG_APP_ID"], "client_secret": env["IG_APP_SECRET"],
        "fb_exchange_token": short}), timeout=30)
    long_tok = j["access_token"]
    pages = http("GET", f"{GRAPH}/me/accounts?" + urllib.parse.urlencode({
        "fields": "name,id,instagram_business_account{id,username}", "access_token": long_tok}), timeout=30).get("data", [])
    linked = [(p["name"], p["instagram_business_account"]) for p in pages if p.get("instagram_business_account")]
    if not linked:
        sys.exit("No Facebook Page with a linked Instagram Business account found. Convert the IG account to Business "
                 "and link it to a Page (Instagram app -> Settings -> Account type and tools / Linked accounts).")
    if len(linked) > 1:
        for i, (pn, ig) in enumerate(linked):
            print(f"  [{i}] @{ig.get('username')} (Page: {pn})")
        pick = int(ask("Which one? ") or 0)
    else:
        pick = 0
    pn, ig = linked[pick]
    update_env(env_path, {"IG_ACCESS_TOKEN": long_tok, "IG_USER_ID": ig["id"], "IG_AUTH_MODE": "facebook_login"})
    save_to_db(db_path, "instagram", long_tok, None, j.get("expires_in", 60 * 86400),
               extra={"ig_user_id": ig["id"], "auth_mode": "facebook_login"})
    print(f"✅ Instagram connected: @{ig.get('username')} (id {ig['id']}) via Page '{pn}'. Token valid ~60 days; "
          "the pipeline refreshes it weekly.")


# ---------------------------------------------------------------------- check
def check(env):
    groups = {"YouTube": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"],
              "Instagram": ["IG_APP_ID", "IG_APP_SECRET", "IG_USER_ID", "IG_ACCESS_TOKEN"],
              "TikTok": ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REDIRECT_URI", "TIKTOK_OPEN_ID",
                         "TIKTOK_ACCESS_TOKEN", "TIKTOK_REFRESH_TOKEN"]}
    for g, ks in groups.items():
        print(f"{g}: " + "  ".join(f"{k}={'set' if env.get(k) else 'MISSING'}" for k in ks))
    print(f"PUBLISH_BACKEND={env.get('PUBLISH_BACKEND', '(default hybrid)')}  PUBLISH_DRY_RUN={env.get('PUBLISH_DRY_RUN', 'true')}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("platform", choices=["youtube", "tiktok", "instagram", "check"])
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--db", default=str(ROOT / "data" / "pipeline.db"))
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    env_path, db_path = Path(a.env), Path(a.db)
    env = read_env(env_path)
    try:
        if a.platform == "check":
            check(env)
        elif a.platform == "youtube":
            youtube_flow(env, env_path, db_path, open_browser=not a.no_browser)
        elif a.platform == "tiktok":
            tiktok_flow(env, env_path, db_path, open_browser=not a.no_browser)
        else:
            instagram_flow(env, env_path, db_path)
    except ApiError as e:
        sys.exit(f"API error: {e}")


if __name__ == "__main__":
    main()
