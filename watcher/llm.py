"""Optional Gemini text calls (chat-spike scoring + hook text). Every function degrades to None."""
import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("llm")
PROMPTS = Path(os.environ.get("PROMPTS_DIR", "/app/prompts"))
MODELS = [os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.5-flash"), "gemini-3.1-flash-lite"]  # fallback on 503s


def _call(system: str, user: str, timeout=30):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    body = json.dumps({"systemInstruction": {"parts": [{"text": system}]},
                       "contents": [{"parts": [{"text": user}]}],
                       "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1024}}).encode()
    for attempt in range(4):
        model = MODELS[attempt % len(MODELS)]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.load(r)
            return j["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            log.warning("gemini call failed (%s): %s", model, e)
            time.sleep(2 * (attempt + 1))
    return None


def score_moment(chat_excerpt: str):
    out = _call((PROMPTS / "gta6_moment_scoring.txt").read_text(), chat_excerpt[:6000])
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


def make_hook(chat_excerpt: str):
    out = _call((PROMPTS / "hook_generator.txt").read_text(), chat_excerpt[:6000])
    if not out:
        return None
    out = out.strip().strip('"\'').splitlines()[0][:60]
    letters = sum(c.isalpha() for c in out)
    if not out or len(out.split()) < 2 or letters < 0.7 * len(out.replace(" ", "")):
        log.warning("discarding malformed hook %r", out)
        return None
    return out


def pick_hashtags(chat_excerpt: str, hook: str):
    """Context-relevant subset of the candidate pool (Spanish-language clip). Returns a list, [] on failure."""
    pool = (PROMPTS / "hashtag_candidates.txt").read_text().split()
    out = _call((PROMPTS / "hashtag_pool.txt").read_text(),
                f"Candidatos: {' '.join(pool)}\nGancho: {hook or '-'}\nChat:\n{chat_excerpt[:5000]}")
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        tags = json.loads(m.group(0)).get("hashtags", []) if m else []
    except json.JSONDecodeError:
        return []
    return [t for t in tags if t in pool][:5]
