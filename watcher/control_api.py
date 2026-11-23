"""Internal control API for n8n (not published to the host). Auth: X-Pipeline-Token == HMAC_SECRET."""
import hmac
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from publishers import dispatcher
from watcher import media, publishing

log = logging.getLogger("api")
TOKEN = os.environ.get("HMAC_SECRET", "")
_ID = r"([a-z0-9]{6})"


def make_server(db, port=8081):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def _auth(self):
            if not TOKEN or not hmac.compare_digest(self.headers.get("X-Pipeline-Token", ""), TOKEN):
                self._send(401, {"error": "unauthorized"}); return False
            return True

        def _file(self, path):
            if not path or not os.path.exists(path):
                return self._send(404, {"error": "file missing"})
            self.send_response(200); self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(os.path.getsize(path))); self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if not self._auth():
                return
            try:
                if m := re.fullmatch(rf"/clip/{_ID}", self.path):
                    return self._send(200, publishing.clip_meta(db, m[1]))
                if m := re.fullmatch(rf"/file/(preview|master)/{_ID}", self.path):
                    meta = publishing.clip_meta(db, m[2])
                    return self._file(meta["preview_path" if m[1] == "preview" else "master_path"])
            except KeyError:
                return self._send(404, {"error": "unknown clip"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            try:
                if m := re.fullmatch(rf"/preview/{_ID}", self.path):
                    meta = publishing.clip_meta(db, m[1])
                    out = os.path.join(os.path.dirname(meta["master_path"]), f"{m[1]}_480p.mp4")
                    media.make_preview(meta["master_path"], out)
                    db.execute("UPDATE clips SET preview_480p_path=? WHERE short_id=?", (out, m[1]))
                    return self._send(200, {"preview_path": out, "bytes": os.path.getsize(out)})
                if m := re.fullmatch(rf"/publish/{_ID}", self.path):
                    return self._send(200, dispatcher.publish(db, m[1]))
                if self.path == "/refresh-tokens":      # n8n weekly cron
                    from db import tokens
                    return self._send(200, tokens.refresh_due(db))
                if self.path == "/cleanup":
                    return self._send(200, publishing.cleanup(db))
            except KeyError:
                return self._send(404, {"error": "unknown clip"})
            except Exception as e:
                log.exception("api error")
                return self._send(500, {"error": str(e)})
            self._send(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="control-api").start()
    log.info("control api on :%d", port)
    return srv
