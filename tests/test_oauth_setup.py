"""scripts/oauth_setup.py helpers and flows with mocked HTTP.   python -m unittest tests.test_oauth_setup -v"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("oauth_setup", Path(__file__).resolve().parent.parent / "scripts" / "oauth_setup.py")
O = importlib.util.module_from_spec(spec)
spec.loader.exec_module(O)


class Http:
    def __init__(self, *resp): self.resp, self.calls = list(resp), []
    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw)); return self.resp.pop(0)


class T(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.addCleanup(self.td.cleanup)
        self.env = Path(self.td.name) / ".env"
        self.env.write_text("A=1\nTIKTOK_CLIENT_KEY=ck\nTIKTOK_CLIENT_SECRET=cs\nTIKTOK_REDIRECT_URI=https://x.io/cb.html\n"
                            "IG_APP_ID=i\nIG_APP_SECRET=s\nTIKTOK_ACCESS_TOKEN=\n")
        self.db = Path(self.td.name) / "none.db"

    def test_update_env_in_place_and_append(self):
        O.update_env(self.env, {"TIKTOK_ACCESS_TOKEN": "tok", "NEW_ONE": "v"})
        t = self.env.read_text()
        self.assertIn("TIKTOK_ACCESS_TOKEN=tok\n", t)
        self.assertIn("A=1\n", t)
        self.assertEqual(t.count("TIKTOK_ACCESS_TOKEN="), 1)
        self.assertTrue(t.rstrip().endswith("NEW_ONE=v"))

    def test_parse_code(self):
        self.assertEqual(O.parse_code("https://x.io/cb.html?code=abc%2Adef&state=s"), "abc*def")
        self.assertEqual(O.parse_code("abc%2Adef"), "abc*def")
        with self.assertRaises(ValueError):
            O.parse_code("https://x.io/cb.html?error=denied&code=")

    def test_tiktok_flow(self):
        env = O.read_env(self.env)
        h = Http({"access_token": "A", "refresh_token": "R", "open_id": "OID", "expires_in": 86400,
                  "scope": "user.info.basic,video.upload,video.list"})
        O.tiktok_flow(env, self.env, self.db, http=h, ask=lambda _: "https://x.io/cb.html?code=C%2A1&state=z", open_browser=False)
        e = O.read_env(self.env)
        self.assertEqual((e["TIKTOK_ACCESS_TOKEN"], e["TIKTOK_REFRESH_TOKEN"], e["TIKTOK_OPEN_ID"]), ("A", "R", "OID"))
        self.assertEqual(h.calls[0][2]["form"]["code"], "C*1")
        self.assertEqual(h.calls[0][2]["form"]["redirect_uri"], "https://x.io/cb.html")

    def test_tiktok_auth_url_has_scopes_without_publish(self):
        u = O.tiktok_auth_url(O.read_env(self.env), "st")
        self.assertIn("scope=user.info.basic%2Cvideo.upload%2Cvideo.list", u)
        self.assertNotIn("video.publish", u)

    def test_instagram_flow_picks_linked_account(self):
        env = O.read_env(self.env)
        h = Http({"access_token": "LONG", "expires_in": 5184000},
                 {"data": [{"name": "NoIG", "id": "1"},
                           {"name": "MyPage", "id": "2", "instagram_business_account": {"id": "1784", "username": "me"}}]})
        O.instagram_flow(env, self.env, self.db, http=h, ask=lambda _: "", ask_secret=lambda _: "SHORT")
        e = O.read_env(self.env)
        self.assertEqual((e["IG_USER_ID"], e["IG_ACCESS_TOKEN"], e["IG_AUTH_MODE"]), ("1784", "LONG", "facebook_login"))
        self.assertIn("fb_exchange_token=SHORT", h.calls[0][1])

    def test_instagram_no_business_account_exits(self):
        h = Http({"access_token": "L"}, {"data": [{"name": "P", "id": "1"}]})
        with self.assertRaises(SystemExit):
            O.instagram_flow(O.read_env(self.env), self.env, self.db, http=h, ask_secret=lambda _: "S")

    def test_missing_creds_exit(self):
        with self.assertRaises(SystemExit):
            O.youtube_flow({}, self.env, self.db, open_browser=False)


if __name__ == "__main__":
    unittest.main()
