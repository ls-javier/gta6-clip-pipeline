"""SQLite OAuth token store: env seeding, auto-refresh, YouTube quota circuit breaker.

`.env` values only seed the table on first boot; afterwards the DB is the source of truth.
`db` is anything with `.execute(sql, params)` and `.query(sql, params)` (db.state.State).

Refresh calls go through an injectable `http(method, url, form=None, timeout=30) -> dict`
so tests never touch the network.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PLATFORMS = ("youtube", "instagram", "tiktok")
IG_GRAPH = "https://graph.facebook.com/v27.0"
IG_BASIC = "https://graph.instagram.com"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

IG_LIFETIME = 60 * 86400
TIKTOK_REFRESH_EVERY = 12 * 3600         # access token lives 24h
IG_REFRESH_EVERY = 7 * 86400
EXPIRY_MARGIN = 300
BREAKER_KEY = "circuit_breaker_until"    # YouTube: unix ts until which uploads are paused


class TokenError(RuntimeError):
    pass


def _now() -> int:
    return int(time.time())


def default_http(method, url, form=None, timeout=30):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if form is not None else {}
    req = urllib.request.Request(url, data, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise TokenError(f"HTTP {e.code} {url.split('?')[0]}: {e.read()[:300].decode('utf-8', 'replace')}") from None


# ------------------------------------------------------------------ storage
def get_token(db, platform) -> dict | None:
    rows = db.query("SELECT * FROM oauth_tokens WHERE platform=?", (platform,))
    return rows[0] if rows else None


def get_extra(db, platform) -> dict:
    row = get_token(db, platform)
    return json.loads(row["extra_json"]) if row and row["extra_json"] else {}


def save_token(db, platform, access, refresh=None, expires_in=None, expires_at=None, extra=None):
    """Upsert. Always overwrites access_token AND refresh_token (TikTok rotates both).
    `extra` is merged into the existing extra_json rather than replacing it."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}")
    if expires_at is None:
        expires_at = _now() + int(expires_in) if expires_in else 0
    merged = {**get_extra(db, platform), **{k: v for k, v in (extra or {}).items() if v is not None}}
    db.execute(
        "INSERT INTO oauth_tokens (platform,access_token,refresh_token,expires_at,extra_json,updated_at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(platform) DO UPDATE SET access_token=excluded.access_token,"
        " refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,"
        " extra_json=excluded.extra_json, updated_at=excluded.updated_at",
        (platform, access or "", refresh, int(expires_at), json.dumps(merged) if merged else None, _now()))


def set_extra(db, platform, **kv):
    row = get_token(db, platform)
    if not row:
        raise TokenError(f"no {platform} token row")
    merged = {**get_extra(db, platform), **kv}
    merged = {k: v for k, v in merged.items() if v is not None}
    db.execute("UPDATE oauth_tokens SET extra_json=? WHERE platform=?",
               (json.dumps(merged) if merged else None, platform))


# ------------------------------------------------------------------ seeding
def seed_from_env(db, env=None) -> list[str]:
    """Insert a row for each platform that has env credentials and no row yet. Returns seeded platforms."""
    env = os.environ if env is None else env
    seeded = []
    if get_token(db, "youtube") is None and env.get("YOUTUBE_REFRESH_TOKEN"):
        save_token(db, "youtube", "", env["YOUTUBE_REFRESH_TOKEN"], expires_at=0)
        seeded.append("youtube")
    if get_token(db, "instagram") is None and env.get("IG_ACCESS_TOKEN"):
        # Real expiry is unknown for a pasted token; assume a fresh 60-day token. The weekly refresh corrects it.
        save_token(db, "instagram", env["IG_ACCESS_TOKEN"], None, expires_in=IG_LIFETIME,
                   extra={"ig_user_id": env.get("IG_USER_ID") or None,
                          "auth_mode": env.get("IG_AUTH_MODE", "facebook_login")})
        seeded.append("instagram")
    if get_token(db, "tiktok") is None and (env.get("TIKTOK_REFRESH_TOKEN") or env.get("TIKTOK_ACCESS_TOKEN")):
        # Unknown access-token age -> expires_at=0 forces a refresh on first use (also rotates the refresh token).
        save_token(db, "tiktok", env.get("TIKTOK_ACCESS_TOKEN", ""), env.get("TIKTOK_REFRESH_TOKEN") or None,
                   expires_at=0, extra={"open_id": env.get("TIKTOK_OPEN_ID") or None})
        seeded.append("tiktok")
    return seeded


# ------------------------------------------------------------------ refresh
def refresh_youtube(db, env=None, http=default_http) -> str:
    env = os.environ if env is None else env
    row = get_token(db, "youtube")
    if not row or not row["refresh_token"]:
        raise TokenError("no YouTube refresh token")
    j = http("POST", GOOGLE_TOKEN_URL, form={
        "client_id": env["YOUTUBE_CLIENT_ID"], "client_secret": env["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": row["refresh_token"], "grant_type": "refresh_token"})
    if "access_token" not in j:
        raise TokenError(f"YouTube refresh failed: {j}")
    # Google does not rotate the refresh token; keep ours unless a new one is returned.
    save_token(db, "youtube", j["access_token"], j.get("refresh_token") or row["refresh_token"],
               expires_in=j.get("expires_in", 3600))
    return j["access_token"]


def refresh_instagram(db, env=None, http=default_http) -> str:
    """Re-mint the long-lived token (60d). facebook_login: fb_exchange_token; instagram_login: ig_refresh_token."""
    env = os.environ if env is None else env
    row = get_token(db, "instagram")
    if not row:
        raise TokenError("no Instagram token")
    mode = get_extra(db, "instagram").get("auth_mode") or env.get("IG_AUTH_MODE", "facebook_login")
    if mode == "instagram_login":
        j = http("GET", f"{IG_BASIC}/refresh_access_token?" + urllib.parse.urlencode(
            {"grant_type": "ig_refresh_token", "access_token": row["access_token"]}))
    else:
        j = http("GET", f"{IG_GRAPH}/oauth/access_token?" + urllib.parse.urlencode(
            {"grant_type": "fb_exchange_token", "client_id": env["IG_APP_ID"],
             "client_secret": env["IG_APP_SECRET"], "fb_exchange_token": row["access_token"]}))
    if "access_token" not in j:
        raise TokenError(f"Instagram refresh failed: {j}")
    save_token(db, "instagram", j["access_token"], row["refresh_token"], expires_in=j.get("expires_in", IG_LIFETIME))
    return j["access_token"]


def refresh_tiktok(db, env=None, http=default_http) -> str:
    env = os.environ if env is None else env
    row = get_token(db, "tiktok")
    if not row or not row["refresh_token"]:
        raise TokenError("no TikTok refresh token")
    j = http("POST", TIKTOK_TOKEN_URL, form={
        "client_key": env["TIKTOK_CLIENT_KEY"], "client_secret": env["TIKTOK_CLIENT_SECRET"],
        "grant_type": "refresh_token", "refresh_token": row["refresh_token"]})
    if "access_token" not in j:
        raise TokenError(f"TikTok refresh failed: {j}")
    # Persist BOTH tokens at once: TikTok may rotate the refresh token, killing the old one.
    save_token(db, "tiktok", j["access_token"], j.get("refresh_token") or row["refresh_token"],
               expires_in=j.get("expires_in", 86400),
               extra={"open_id": j.get("open_id") or None})
    return j["access_token"]


REFRESHERS = {"youtube": refresh_youtube, "instagram": refresh_instagram, "tiktok": refresh_tiktok}
REFRESH_EVERY = {"youtube": None, "instagram": IG_REFRESH_EVERY, "tiktok": TIKTOK_REFRESH_EVERY}


def get_valid_access_token(db, platform, env=None, http=default_http) -> str:
    """Return a non-expired access token, refreshing on demand."""
    row = get_token(db, platform)
    if row is None:
        seed_from_env(db, env)
        row = get_token(db, platform)
    if row is None:
        raise TokenError(f"no {platform} credentials configured")
    if row["access_token"] and row["expires_at"] > _now() + EXPIRY_MARGIN:
        return row["access_token"]
    return REFRESHERS[platform](db, env, http)


def refresh_due(db, env=None, http=default_http, force=False) -> dict:
    """Cron entrypoint (n8n, every 12h). Seeds from env, then refreshes whatever is due:
    TikTok every 12h, Instagram every 7d, YouTube when the access token is (nearly) expired.
    Returns {platform: 'refreshed' | 'skipped' | 'error: ...'}."""
    seed_from_env(db, env)
    out = {}
    for p in PLATFORMS:
        row = get_token(db, p)
        if row is None:
            continue
        every = REFRESH_EVERY[p]
        if every is None:
            due = not row["access_token"] or row["expires_at"] <= _now() + EXPIRY_MARGIN
        else:
            due = force or _now() - row["updated_at"] >= every or row["expires_at"] <= _now() + EXPIRY_MARGIN
        if not due and not force:
            out[p] = "skipped"
            continue
        try:
            REFRESHERS[p](db, env, http)
            out[p] = "refreshed"
        except Exception as e:                      # one platform failing must not block the others
            out[p] = f"error: {str(e)[:200]}"
    return out


# ------------------------------------------------- YouTube quota circuit breaker
def next_quota_reset(now: int | None = None) -> int:
    """YouTube quota resets at midnight Pacific Time. Returns that instant (+60s margin) as unix ts."""
    pt = ZoneInfo("America/Los_Angeles")
    t = datetime.fromtimestamp(now if now is not None else _now(), pt)
    nxt = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(nxt.timestamp()) + 60


def trip_youtube_breaker(db, now: int | None = None) -> int:
    """Call on any 403 quotaExceeded: pauses uploads (and retries) until the next daily window."""
    until = next_quota_reset(now)
    if get_token(db, "youtube") is None:
        save_token(db, "youtube", "", None, expires_at=0)
    set_extra(db, "youtube", **{BREAKER_KEY: until})
    return until


def youtube_paused_until(db, now: int | None = None) -> int:
    """0 if uploads are allowed, else the unix ts when they resume."""
    until = int(get_extra(db, "youtube").get(BREAKER_KEY) or 0)
    return until if until > (now if now is not None else _now()) else 0


def clear_youtube_breaker(db):
    if get_token(db, "youtube"):
        set_extra(db, "youtube", **{BREAKER_KEY: None})


if __name__ == "__main__":          # python -m db.tokens  -> cron refresh against DB_PATH
    from db.state import State
    _db = State(os.environ.get("DB_PATH", "/data/pipeline.db"))
    print(json.dumps(refresh_due(_db)))
