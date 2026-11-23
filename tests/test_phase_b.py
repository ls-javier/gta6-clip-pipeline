"""Phase B: native publishers (mocked HTTP), TikTok discovery, dispatcher bookkeeping.
python -m unittest tests.test_phase_b -v"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from db import tokens
from db.state import State
from monetization import campaign_cli
from publishers import dispatcher, instagram_pub, tiktok_inbox_pub, youtube_pub
from publishers._http import ApiError

ENV = {"YOUTUBE_CLIENT_ID": "i", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r",
       "IG_APP_ID": "a", "IG_APP_SECRET": "b", "IG_USER_ID": "u1", "IG_ACCESS_TOKEN": "igt",
       "TIKTOK_CLIENT_KEY": "k", "TIKTOK_CLIENT_SECRET": "s", "TIKTOK_REFRESH_TOKEN": "tr"}


class Rec:
    """Scripted http: pops (matcher substring, response); records calls."""
    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        sub, resp = self.script.pop(0)
        assert sub in url, f"expected {sub!r} in {url!r}"
        if isinstance(resp, Exception):
            raise resp
        return resp


class Base(unittest.TestCase):
    def setUp(self):
        self.db = State(":memory:")
        self.addCleanup(self.db.close)
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.mp4 = Path(self.td.name) / "m.mp4"
        self.mp4.write_bytes(b"\x00" * 2048)
        self.meta = {"master_path": str(self.mp4), "yt_title": "Wow @s #Shorts", "yt_description": "d", "caption": "cap #gta6"}
        tokens.seed_from_env(self.db, ENV)
        for p in ("youtube", "instagram", "tiktok"):         # pretend tokens are fresh -> no refresh traffic
            self.db.execute("UPDATE oauth_tokens SET access_token='acc', expires_at=? WHERE platform=?", (time.time() + 9999, p))


class YouTube(Base):
    def test_payload_and_success(self):
        h = Rec(("uploadType=resumable", ({}, {"Location": "https://up/loc"})),
                ("https://up/loc", {"id": "VID", "status": {"privacyStatus": "public"}}))
        r = youtube_pub.upload(self.db, self.meta, False, http=h)
        self.assertEqual((r["success"], r["url"], r["video_id"], r["note"]), (True, "https://youtube.com/shorts/VID", "VID", None))
        body = h.calls[0][2]["data"]
        self.assertEqual(body["status"], {"privacyStatus": "public", "selfDeclaredMadeForKids": False})
        self.assertEqual(body["paidProductPlacementDetails"], {"hasPaidProductPlacement": False})
        self.assertIn("paidProductPlacementDetails", h.calls[0][1])

    def test_dry_run_private_and_audit_lock_note(self):
        self.assertEqual(youtube_pub.build_body(self.meta, True)["status"]["privacyStatus"], "private")
        h = Rec(("resumable", ({}, {"location": "https://up/loc"})), ("up/loc", {"id": "V", "status": {"privacyStatus": "private"}}))
        r = youtube_pub.upload(self.db, self.meta, False, http=h)
        self.assertIn("locked", r["note"])

    def test_quota_exceeded_trips_breaker_then_blocks_without_http(self):
        h = Rec(("resumable", ApiError('HTTP 403: {"reason": "quotaExceeded"}')))
        r = youtube_pub.upload(self.db, self.meta, False, http=h)
        self.assertFalse(r["success"])
        self.assertGreater(tokens.youtube_paused_until(self.db), time.time())
        h2 = Rec()
        r2 = youtube_pub.upload(self.db, self.meta, False, http=h2)
        self.assertEqual((r2["success"], h2.calls), (False, []))

    def test_other_errors_propagate_without_tripping(self):
        with self.assertRaises(ApiError):
            youtube_pub.upload(self.db, self.meta, False, http=Rec(("resumable", ApiError("HTTP 500: boom"))))
        self.assertEqual(tokens.youtube_paused_until(self.db), 0)


class Instagram(Base):
    def test_three_step_flow(self):
        h = Rec(("/v27.0/u1/media", {"id": "C1", "uri": "https://rupload.facebook.com/x"}),
                ("rupload.facebook.com", {}),
                ("fields=status_code", {"status_code": "IN_PROGRESS"}),
                ("fields=status_code", {"status_code": "FINISHED"}),
                ("media_publish", {"id": "M1"}),
                ("fields=permalink", {"permalink": "https://instagram.com/reel/abc"}))
        r = instagram_pub.upload(self.db, self.meta, False, http=h, env=ENV, sleep=lambda s: None)
        self.assertEqual((r["success"], r["url"], r["video_id"]), (True, "https://instagram.com/reel/abc", "M1"))
        self.assertEqual(h.calls[0][2]["form"]["upload_type"], "resumable")
        self.assertEqual(h.calls[1][2]["headers"]["file_size"], "2048")

    def test_dry_run_skipped_and_error_status(self):
        self.assertTrue(instagram_pub.upload(self.db, self.meta, True, http=Rec(), env=ENV).get("skipped"))
        h = Rec(("/media", {"id": "C", "uri": "https://r"}), ("https://r", {}), ("status_code", {"status_code": "ERROR"}))
        with self.assertRaises(ApiError):
            instagram_pub.upload(self.db, self.meta, False, http=h, env=ENV, sleep=lambda s: None)


class TikTok(Base):
    def test_inbox_upload_and_caption_note(self):
        h = Rec(("inbox/video/init", {"data": {"upload_url": "https://tt/up"}, "error": {"code": "ok"}}), ("https://tt/up", {}))
        r = tiktok_inbox_pub.upload(self.db, self.meta, False, http=h, env=ENV)
        self.assertTrue(r["success"])
        self.assertIn("cap #gta6", r["note"])
        self.assertEqual(h.calls[0][2]["data"]["source_info"]["source"], "FILE_UPLOAD")

    def test_draft_cap(self):
        self.db.add_streamer("a", "@a")
        for _ in range(5):
            sid = self.db.new_clip(1, 1.0)
            self.db.execute("INSERT INTO publications (clip_short_id,platform,published_at,via) VALUES (?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),'native')",
                            (sid, "tiktok"))
        r = tiktok_inbox_pub.upload(self.db, self.meta, False, http=Rec(), env=ENV)
        self.assertFalse(r["success"])

    def test_discovery_polls_then_finds_new_video(self):
        now = int(time.time())
        empty = {"data": {"videos": [{"id": "old", "create_time": now - 5000, "share_url": "x"}]}}
        found = {"data": {"videos": [{"id": "old", "create_time": now - 5000}, {"id": "new", "create_time": now + 5, "share_url": "https://tiktok.com/@a/video/1"}]}}
        h = Rec(("video/list", empty), ("video/list", ApiError("HTTP 500")), ("video/list", found))
        got, sleeps = [], []
        v = tiktok_inbox_pub.discover(self.db, "abc123", now, got.append, http=h, env=ENV, sleep=sleeps.append)
        self.assertEqual(v["id"], "new")
        self.assertEqual((got[0]["share_url"], sleeps), ("https://tiktok.com/@a/video/1", [180, 180]))

    def test_discovery_times_out_after_30_min(self):
        h = Rec(*[("video/list", {"data": {"videos": []}})] * 11)
        sent = []
        with mock.patch("publishers.notify.send", lambda t, *a, **k: sent.append(t)):
            v = tiktok_inbox_pub.discover(self.db, "abc123", int(time.time()), lambda v: None, http=h, env=ENV, sleep=lambda s: None)
        self.assertIsNone(v)
        self.assertEqual(len(h.calls), 11)               # t=0,3,...,30 min
        self.assertTrue(sent)


class Dispatcher(Base):
    def setUp(self):
        super().setUp()
        self.db.add_streamer("shroud", "@shroud")
        self.sid = self.db.new_clip(1, 1.0)

    def test_record_publication_with_campaign_and_deadline(self):
        campaign_cli.add_campaign(self.db, name="C", model="cpm", cpm=1, streamer="shroud", platforms="youtube,tiktok")
        pid = dispatcher.record_publication(self.db, self.sid, "youtube", "https://y/1", "V1", now=1000)
        row = self.db.query("SELECT * FROM publications_v2 WHERE id=?", (pid,))[0]
        self.assertEqual((row["campaign_id"], row["submit_deadline_at"], row["bounty_status"]), (1, 1000 + 1800, "pending_submission"))
        # platform not allowed by the campaign -> no campaign
        pid = dispatcher.record_publication(self.db, self.sid, "instagram", "https://i/1", now=1000)
        row = self.db.query("SELECT * FROM publications_v2 WHERE id=?", (pid,))[0]
        self.assertEqual((row["campaign_id"], row["bounty_status"]), (None, "not_applicable"))

    def test_dry_run_not_tracked_live_is_tracked(self):
        res = {"dry_run": True, "details": {"youtube": {"url": "u", "video_id": "v", "via": "upload_post"}}}
        with mock.patch("publishers.dispatcher.publishing.publish", return_value=res):
            dispatcher.publish(self.db, self.sid)
        self.assertEqual(self.db.query("SELECT COUNT(*) n FROM publications_v2")[0]["n"], 0)
        res = {"dry_run": False, "details": {"youtube": {"url": "u", "video_id": "v", "via": "upload_post"},
                                              "tiktok": {"url": "t", "video_id": None, "via": "upload_post"}}}
        with mock.patch("publishers.dispatcher.publishing.publish", return_value=res):
            dispatcher.publish(self.db, self.sid)
        self.assertEqual(self.db.query("SELECT COUNT(*) n FROM publications_v2")[0]["n"], 2)

    def test_native_tiktok_without_url_starts_watcher(self):
        res = {"dry_run": False, "details": {"tiktok": {"url": None, "video_id": None, "via": "native"}}}
        with mock.patch("publishers.dispatcher.publishing.publish", return_value=res), \
                mock.patch("publishers.tiktok_inbox_pub.start_watch") as sw:
            dispatcher.publish(self.db, self.sid)
        sw.assert_called_once()
        self.assertEqual(self.db.query("SELECT COUNT(*) n FROM publications_v2")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
