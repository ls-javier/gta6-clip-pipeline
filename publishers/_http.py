"""Tiny urllib JSON client shared by the native publishers (injectable in tests)."""
import json
import urllib.error
import urllib.parse
import urllib.request


class ApiError(RuntimeError):
    pass


def http(method, url, *, data=None, headers=None, form=None, timeout=600, want_headers=False):
    h = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json; charset=UTF-8")
    req = urllib.request.Request(url, data, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            body = json.loads(raw) if raw and raw.lstrip()[:1] in (b"{", b"[") else {}
            return (body, dict(r.headers)) if want_headers else body
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} {url.split('?')[0]}: {e.read()[:400].decode('utf-8', 'replace')}") from None
