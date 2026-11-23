"""Compatibility shim: native uploaders now live in publishers/ (youtube_pub, instagram_pub, tiktok_inbox_pub).
Each uploader has signature (db, meta, dry) -> {"success","url","error",...}. Tokens live in db/tokens.py."""
from db import tokens
from publishers import instagram_pub, tiktok_inbox_pub, youtube_pub

UPLOADERS = {"youtube": (youtube_pub.configured, youtube_pub.upload),
             "instagram": (instagram_pub.configured, instagram_pub.upload),
             "tiktok": (tiktok_inbox_pub.configured, tiktok_inbox_pub.upload)}


def configured_platforms():
    return [p for p, (ok, _) in UPLOADERS.items() if ok()]


def refresh_tokens(db):
    return tokens.refresh_due(db)
