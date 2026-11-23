"""Phase A: schema migration, token store (mocked HTTP), campaign CLI.   python -m unittest tests.test_phase_a -v"""
import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from db import tokens
from db.state import State
from monetization import campaign_cli

ENV = {"YOUTUBE_CLIENT_ID": "yid", "YOUTUBE_CLIENT_SECRET": "ysec", "YOUTUBE_REFRESH_TOKEN": "yref",
       "IG_APP_ID": "iid", "IG_APP_SECRET": "isec", "IG_USER_ID": "17841", "IG_ACCESS_TOKEN": "ig-old",
       "TIKTOK_CLIENT_KEY": "tk", "TIKTOK_CLIENT_SECRET": "ts", "TIKTOK_OPEN_ID": "oid",
       "TIKTOK_ACCESS_TOKEN": "tt-acc-0", "TIKTOK_REFRESH_TOKEN": "tt-ref-0"}


class FakeHttp:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, form=None, timeout=30):
        self.calls.append((method, url, form))
        return self.responses.pop(0)


class SchemaTests(unittest.TestCase):
    def test_tables_and_idempotent(self):
        db = State(":memory:")
        self.addCleanup(db.close)
        db._conn.executescript(Path("db/schema_monetization.sql").read_text())   # re-apply: no error
        names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"oauth_tokens", "campaigns", "publications_v2", "metric_snapshots",
                         "payout_rounds", "streamers", "clips"} <= names)

    def test_legacy_oauth_table_migrated(self):
        with tempfile.TemporaryDirectory() as td:
            path = f"{td}/p.db"
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE oauth_tokens (provider TEXT PRIMARY KEY, access_token TEXT, refresh_token TEXT,"
                      " expires_at TEXT, updated_at TEXT)")
            c.execute("INSERT INTO oauth_tokens VALUES ('tiktok','A','R','2030-01-01T00:00:00Z','x')")
            c.execute("INSERT INTO oauth_tokens VALUES ('youtube',NULL,'R2',NULL,'x')")
            c.commit(); c.close()
            db = State(path)
            rows = {r["platform"]: r for r in db.query("SELECT * FROM oauth_tokens")}
            self.assertEqual(rows["tiktok"]["access_token"], "A")
            self.assertEqual(rows["tiktok"]["expires_at"], 1893456000)
            self.assertNotIn("youtube", rows)            # empty-access legacy row is re-seedable from env
            db.close()


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.db = State(":memory:")
        self.addCleanup(self.db.close)

    def test_seed_once_db_wins(self):
        self.assertEqual(sorted(tokens.seed_from_env(self.db, ENV)), ["instagram", "tiktok", "youtube"])
        tokens.save_token(self.db, "tiktok", "NEW", "NEWREF", expires_in=100)
        self.assertEqual(tokens.seed_from_env(self.db, ENV), [])
        self.assertEqual(tokens.get_token(self.db, "tiktok")["refresh_token"], "NEWREF")
        self.assertEqual(tokens.get_extra(self.db, "tiktok")["open_id"], "oid")      # extra survives overwrite
        self.assertEqual(tokens.get_extra(self.db, "instagram")["ig_user_id"], "17841")

    def test_tiktok_refresh_token_rotation_persisted(self):
        tokens.seed_from_env(self.db, ENV)
        http = FakeHttp({"access_token": "tt-acc-1", "refresh_token": "tt-ref-1", "expires_in": 86400})
        self.assertEqual(tokens.refresh_tiktok(self.db, ENV, http), "tt-acc-1")
        row = tokens.get_token(self.db, "tiktok")
        self.assertEqual((row["access_token"], row["refresh_token"]), ("tt-acc-1", "tt-ref-1"))
        self.assertGreater(row["expires_at"], time.time() + 86000)
        self.assertEqual(http.calls[0][2]["refresh_token"], "tt-ref-0")
        # second refresh must use the ROTATED token
        http2 = FakeHttp({"access_token": "tt-acc-2", "refresh_token": "tt-ref-2", "expires_in": 86400})
        tokens.refresh_tiktok(self.db, ENV, http2)
        self.assertEqual(http2.calls[0][2]["refresh_token"], "tt-ref-1")
        self.assertEqual(tokens.get_token(self.db, "tiktok")["refresh_token"], "tt-ref-2")
        self.assertEqual(tokens.get_extra(self.db, "tiktok")["open_id"], "oid")

    def test_tiktok_refresh_keeps_old_refresh_if_not_returned_and_errors_dont_clobber(self):
        tokens.seed_from_env(self.db, ENV)
        tokens.refresh_tiktok(self.db, ENV, FakeHttp({"access_token": "a", "expires_in": 86400}))
        self.assertEqual(tokens.get_token(self.db, "tiktok")["refresh_token"], "tt-ref-0")
        with self.assertRaises(tokens.TokenError):
            tokens.refresh_tiktok(self.db, ENV, FakeHttp({"error": "invalid_grant"}))
        self.assertEqual(tokens.get_token(self.db, "tiktok")["access_token"], "a")

    def test_youtube_refresh(self):
        tokens.seed_from_env(self.db, ENV)
        tok = tokens.get_valid_access_token(self.db, "youtube", ENV, FakeHttp({"access_token": "ya", "expires_in": 3599}))
        self.assertEqual(tok, "ya")
        # still fresh -> no HTTP call
        self.assertEqual(tokens.get_valid_access_token(self.db, "youtube", ENV, FakeHttp()), "ya")
        self.assertEqual(tokens.get_token(self.db, "youtube")["refresh_token"], "yref")

    def test_instagram_refresh_modes(self):
        tokens.seed_from_env(self.db, ENV)
        http = FakeHttp({"access_token": "ig-new", "expires_in": 5184000})
        tokens.refresh_instagram(self.db, ENV, http)
        self.assertIn("graph.facebook.com/v27.0/oauth/access_token", http.calls[0][1])
        self.assertIn("fb_exchange_token=ig-old", http.calls[0][1])
        self.assertEqual(tokens.get_token(self.db, "instagram")["access_token"], "ig-new")
        self.assertEqual(tokens.get_extra(self.db, "instagram")["ig_user_id"], "17841")

    def test_refresh_due_schedule(self):
        tokens.seed_from_env(self.db, ENV)
        http = FakeHttp({"access_token": "ya", "expires_in": 3600},
                        {"access_token": "tt1", "refresh_token": "r1", "expires_in": 86400})
        out = tokens.refresh_due(self.db, ENV, http)          # TikTok (expires_at=0) + YouTube due; IG fresh
        self.assertEqual(out, {"youtube": "refreshed", "instagram": "skipped", "tiktok": "refreshed"})
        self.assertEqual(tokens.refresh_due(self.db, ENV, FakeHttp()),
                         {"youtube": "skipped", "instagram": "skipped", "tiktok": "skipped"})
        # 13h later TikTok is due again even though its access token is still valid; IG not yet (7d)
        self.db.execute("UPDATE oauth_tokens SET updated_at=updated_at-13*3600")
        out = tokens.refresh_due(self.db, ENV, FakeHttp({"access_token": "tt2", "refresh_token": "r2", "expires_in": 86400}))
        self.assertEqual(out["tiktok"], "refreshed")
        self.assertEqual(out["instagram"], "skipped")
        # 8 days later IG is due
        self.db.execute("UPDATE oauth_tokens SET updated_at=updated_at-8*86400")
        out = tokens.refresh_due(self.db, ENV, FakeHttp({"access_token": "ig2", "expires_in": 5184000},
                                                        {"access_token": "t", "refresh_token": "r", "expires_in": 86400}))
        self.assertEqual(out["instagram"], "refreshed")

    def test_one_platform_error_does_not_block_others(self):
        tokens.seed_from_env(self.db, ENV)
        http = FakeHttp({"error": "bad"}, {"access_token": "t", "refresh_token": "r", "expires_in": 86400})
        out = tokens.refresh_due(self.db, ENV, http)             # youtube fails first, tiktok must still run
        self.assertTrue(out["youtube"].startswith("error"))
        self.assertEqual(out["tiktok"], "refreshed")

    def test_youtube_circuit_breaker(self):
        tokens.seed_from_env(self.db, ENV)
        self.assertEqual(tokens.youtube_paused_until(self.db), 0)
        pt = ZoneInfo("America/Los_Angeles")
        now = int(datetime(2026, 11, 22, 15, 30, tzinfo=pt).timestamp())
        until = tokens.trip_youtube_breaker(self.db, now)
        self.assertEqual(datetime.fromtimestamp(until - 60, pt), datetime(2026, 11, 23, 0, 0, tzinfo=pt))
        self.assertEqual(tokens.youtube_paused_until(self.db, now), until)
        self.assertEqual(tokens.youtube_paused_until(self.db, until + 1), 0)
        # breaker flag survives a token refresh
        tokens.refresh_youtube(self.db, ENV, FakeHttp({"access_token": "ya", "expires_in": 3600}))
        self.assertEqual(tokens.get_extra(self.db, "youtube")[tokens.BREAKER_KEY], until)
        tokens.clear_youtube_breaker(self.db)
        self.assertEqual(tokens.youtube_paused_until(self.db, now), 0)


class CampaignCliTests(unittest.TestCase):
    def setUp(self):
        self.db = State(":memory:")
        self.addCleanup(self.db.close)
        self.db.add_streamer("shroud", "@shroud")

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = campaign_cli.main(list(argv), db=self.db)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_sample_gta6_cpm_campaign(self):
        out = self.run_cli("add", "--name", "GTA VI Clips", "--model", "cpm", "--cpm", "1.00", "--cap", "200",
                           "--tags", "#gta6 #gtavi", "--streamer", "shroud")
        self.assertIn("$1.00 CPM", out)
        c = campaign_cli.get_campaign(self.db, 1)
        self.assertEqual((c["cpm_rate_usd"], c["max_payout_cap_usd"], c["creator_fee_rate"]), (1.0, 200.0, 0.10))
        self.assertEqual(c["twitch_channel"], "shroud")
        self.assertEqual(json.loads(c["allowed_platforms"]), ["tiktok", "instagram", "youtube"])
        self.assertEqual((c["submission_window_min"], c["status"]), (30, "active"))

    def test_fee_rules(self):
        f = campaign_cli.fee_rate_for
        self.assertEqual(f("cpm", 50000), 0.10)
        self.assertEqual(f("pool", 50000), 0.10)
        self.assertEqual(f("per_post", 4999.99), 0.10)
        self.assertEqual(f("per_post", 5000), 0.0)
        self.assertEqual(f("retainer", 9000), 0.0)

    def test_edit_pause_link_list(self):
        self.run_cli("add", "--name", "Retainer", "--model", "retainer", "--flat-fee", "500", "--budget", "1000")
        self.assertEqual(campaign_cli.get_campaign(self.db, 1)["creator_fee_rate"], 0.10)
        self.run_cli("edit", "1", "--budget", "6000")                       # fee re-derived
        self.assertEqual(campaign_cli.get_campaign(self.db, 1)["creator_fee_rate"], 0.0)
        self.run_cli("edit", "1", "--platforms", "youtube,tiktok", "--cap", "300")
        c = campaign_cli.get_campaign(self.db, 1)
        self.assertEqual((json.loads(c["allowed_platforms"]), c["max_payout_cap_usd"]), (["youtube", "tiktok"], 300.0))
        self.run_cli("pause", "1")
        self.assertEqual(campaign_cli.get_campaign(self.db, 1)["status"], "paused")
        self.assertEqual(self.run_cli("list", "--status", "active"), "")
        self.run_cli("resume", "1")
        self.run_cli("link", "1", "#Shroud")
        self.assertEqual(campaign_cli.get_campaign(self.db, 1)["streamer_id"], 1)
        self.assertIn("Retainer", self.run_cli("list"))

    def test_validation(self):
        for argv in (["add", "--name", "x", "--model", "cpm"],                                  # cpm missing
                     ["add", "--name", "x", "--model", "per_post"],                             # flat fee missing
                     ["add", "--name", "x", "--model", "cpm", "--cpm", "1", "--platforms", "x"],
                     ["add", "--name", "x", "--model", "cpm", "--cpm", "1", "--streamer", "nobody"],
                     ["pause", "99"]):
            with self.assertRaises(SystemExit, msg=argv), contextlib.redirect_stderr(io.StringIO()):
                campaign_cli.main(argv, db=self.db)


if __name__ == "__main__":
    unittest.main()
