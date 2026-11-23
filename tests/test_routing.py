"""Hybrid publish routing.   python -m unittest tests.test_routing -v"""
import unittest
from datetime import datetime, timezone

from db.state import State
from watcher.publishing import route_platforms, upload_post_used_this_month, _looks_like_quota

ALL = ["tiktok", "instagram", "youtube"]


class Routing(unittest.TestCase):
    def test_tiktok_native_by_default_even_when_quota_exhausted(self):
        r, s = route_platforms("hybrid", ALL, ALL, ALL, used=10, limit=10)
        self.assertEqual(r, {"tiktok": "native", "instagram": "native", "youtube": "native"})

    def test_tiktok_upload_post_when_configured(self):
        r, _ = route_platforms("hybrid", ALL, ALL, ["youtube", "instagram"], used=10, limit=10, tiktok_route="upload_post")
        self.assertEqual(r["tiktok"], "upload_post")

    def test_yt_ig_use_upload_post_while_allowance_remains(self):
        r, _ = route_platforms("hybrid", ALL, ALL, ["youtube", "instagram"], used=0, limit=10)
        self.assertEqual(r, {"instagram": "upload_post", "youtube": "upload_post"})

    def test_allowance_runs_out_mid_publish(self):
        r, _ = route_platforms("hybrid", ALL, ALL, ["youtube", "instagram"], used=9, limit=10)
        self.assertEqual(r, {"instagram": "upload_post", "youtube": "native"})

    def test_exhausted_without_native_creds_is_skipped(self):
        r, s = route_platforms("hybrid", ALL, ALL, [], used=10, limit=10)
        self.assertEqual(r, {})
        self.assertIn("youtube", s)

    def test_tiktok_without_native_creds_is_skipped(self):
        r, s = route_platforms("hybrid", ALL, ALL, ["youtube"], used=0, limit=10)
        self.assertNotIn("tiktok", r)
        self.assertIn("tiktok", s)

    def test_monthly_count_resets_and_ignores_tiktok_and_native(self):
        db = State(":memory:")
        self.addCleanup(db.close)
        db.add_streamer("a", "@a")
        for i, (plat, via, ts) in enumerate([("youtube", "upload_post", "2026-11-03T10:00:00Z"),
                                             ("instagram", "upload_post", "2026-11-04T10:00:00Z"),
                                             ("tiktok", "upload_post", "2026-11-04T10:00:00Z"),
                                             ("youtube", "native", "2026-11-05T10:00:00Z"),
                                             ("youtube", "upload_post", "2026-10-30T10:00:00Z")]):
            sid = db.new_clip(1, 1.0)
            db.execute("INSERT INTO publications (clip_short_id,platform,published_at,via) VALUES (?,?,?,?)",
                       (sid, plat, ts, via))
        self.assertEqual(upload_post_used_this_month(db, datetime(2026, 11, 22, tzinfo=timezone.utc)), 2)

    def test_quota_error_detection(self):
        self.assertTrue(_looks_like_quota('HTTP 403: {"upgrade_url": "..."}'))
        self.assertFalse(_looks_like_quota("HTTP 500: boom"))


if __name__ == "__main__":
    unittest.main()
