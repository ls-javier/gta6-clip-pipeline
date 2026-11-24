"""Thread-safe SQLite helper for pipeline.db."""
import secrets
import sqlite3
import string
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_MONETIZATION = Path(__file__).with_name("schema_monetization.sql")
_ALPHABET = string.ascii_lowercase + string.digits


class State:
    def __init__(self, path: str = "pipeline.db"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA.read_text())
            self._drop_legacy_oauth_tokens()
            self._conn.executescript(SCHEMA_MONETIZATION.read_text())
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(clips)")}
            if "via" not in {r[1] for r in self._conn.execute("PRAGMA table_info(publications)")}:
                self._conn.execute("ALTER TABLE publications ADD COLUMN via TEXT")
            scols = {r[1] for r in self._conn.execute("PRAGMA table_info(streamers)")}
            if "spike_ratio_threshold" not in scols:
                self._conn.execute("ALTER TABLE streamers ADD COLUMN spike_ratio_threshold REAL")
            if "mode" not in scols:
                self._conn.execute("ALTER TABLE streamers ADD COLUMN mode TEXT NOT NULL DEFAULT 'always'")
            if "layout" not in cols:                        # per-clip framing chosen from Telegram (NULL = default)
                self._conn.execute("ALTER TABLE clips ADD COLUMN layout TEXT")
            if "recut_prompt" not in cols:                  # free-text changes requested from Telegram + parsed params
                self._conn.execute("ALTER TABLE clips ADD COLUMN recut_prompt TEXT")
                self._conn.execute("ALTER TABLE clips ADD COLUMN recut_params TEXT")
            if "hashtags" not in cols:                      # migrate DBs created before hashtags existed
                self._conn.execute("ALTER TABLE clips ADD COLUMN hashtags TEXT")

    def _drop_legacy_oauth_tokens(self):
        """Early prototype keyed oauth_tokens by `provider` (ISO-text expiry). Superseded by the
        `platform`/unix-epoch table in schema_monetization.sql; carry any rows across."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(oauth_tokens)")}
        if "provider" not in cols:
            return
        old = self._conn.execute("SELECT * FROM oauth_tokens").fetchall()
        self._conn.execute("DROP TABLE oauth_tokens")
        self._conn.executescript(SCHEMA_MONETIZATION.read_text())
        for r in old:
            if not r["access_token"]:
                continue
            try:
                exp = int(datetime.strptime(r["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
                          .replace(tzinfo=timezone.utc).timestamp())
            except (TypeError, ValueError):
                exp = 0
            self._conn.execute(
                "INSERT OR IGNORE INTO oauth_tokens (platform,access_token,refresh_token,expires_at,updated_at)"
                " VALUES (?,?,?,?,?)", (r["provider"], r["access_token"], r["refresh_token"], exp, int(time.time())))

    def execute(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def add_streamer(self, twitch_channel, credit_handle, **kw) -> int:
        cols = ["twitch_channel", "credit_handle", *kw]
        sql = f"INSERT INTO streamers ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})"
        return self.execute(sql, [twitch_channel, credit_handle, *kw.values()]).lastrowid

    def new_clip(self, streamer_id, spike_ts, spike_ratio=None, chat_excerpt=None, raw_path=None) -> str:
        for _ in range(10):
            sid = "".join(secrets.choice(_ALPHABET) for _ in range(6))
            try:
                self.execute(
                    "INSERT INTO clips (short_id,streamer_id,spike_ts,spike_ratio,chat_excerpt,raw_path)"
                    " VALUES (?,?,?,?,?,?)",
                    (sid, streamer_id, spike_ts, spike_ratio, chat_excerpt, raw_path))
                return sid
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("could not allocate short_id")

    def set_status(self, short_id, status):
        self.execute("UPDATE clips SET status=? WHERE short_id=?", (status, short_id))

    def close(self):
        with self._lock:
            self._conn.close()
