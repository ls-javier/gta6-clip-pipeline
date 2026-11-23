"""Official Twitch Helix client (app access token). Needs TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET (free:
https://dev.twitch.tv/console/apps). Used only to rank 'gta6_only' streamers by viewers and check the game."""
import json
import logging
import os
import time
import urllib.parse
import urllib.request

log = logging.getLogger("twitch")
_tok = {"v": None, "exp": 0}


def configured() -> bool:
    return bool(os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"))


def _token():
    if _tok["v"] and time.time() < _tok["exp"] - 60:
        return _tok["v"]
    data = urllib.parse.urlencode({"client_id": os.environ["TWITCH_CLIENT_ID"], "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
                                   "grant_type": "client_credentials"}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://id.twitch.tv/oauth2/token", data), timeout=20) as r:
        j = json.load(r)
    _tok.update(v=j["access_token"], exp=time.time() + j["expires_in"])
    return _tok["v"]


def live_streams(logins):
    """{login: {'viewers': int, 'game': str}} for channels currently live."""
    q = urllib.parse.urlencode([("user_login", l) for l in logins] + [("first", "100")])
    req = urllib.request.Request(f"https://api.twitch.tv/helix/streams?{q}",
                                 headers={"Client-Id": os.environ["TWITCH_CLIENT_ID"], "Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)["data"]
    return {s["user_login"].lower(): {"viewers": s["viewer_count"], "game": s.get("game_name", "")} for s in data if s["type"] == "live"}


def is_gta6(game: str) -> bool:
    """Matches the GTA VI category but not GTA V ('Grand Theft Auto V' is a prefix of '... VI')."""
    g = " ".join((game or "").lower().replace(":", " ").split())
    return g in ("grand theft auto vi", "grand theft auto 6", "gta vi", "gta 6") or g.startswith(("grand theft auto vi ", "grand theft auto 6 "))
