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


_RECUT_SYSTEM = """Convierte las instrucciones del usuario para rehacer un clip de Twitch en JSON. Devuelve SOLO un objeto JSON con estas claves (null si no se pide cambio):
- "layout": "a" (juego horizontal arriba + cara del streamer abajo), "b" (cara + centro del juego), "c" (automatico) o null
- "hook_text": texto nuevo del gancho para titulo/descripcion (max 60 caracteres, en espanol) o null
- "auto_hook": false si pide quitar el gancho/texto superpuesto, true si pide ponerlo, si no null
- "captions": false si pide quitar subtitulos, true si pide ponerlos, si no null
- "clip_min", "clip_max": duracion deseada en segundos (enteros) o null
- "trim_start": segundos a quitar del principio del clip original o null
- "trim_end": segundos a quitar del final o null
- "audio": "strict" (quitar musica/solo voz) o "balanced" (mas sonido del juego) o null
- "unapplied": lo que el usuario pidio y NO encaja en las claves anteriores (texto breve) o null
"""


def parse_recut(prompt: str):
    """Free-text recut request -> params dict. Falls back to keyword rules when Gemini is down."""
    out = _call(_RECUT_SYSTEM, prompt[:1500])
    m = re.search(r"\{.*\}", out or "", re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return _clean_recut(d)
        except json.JSONDecodeError:
            pass
    low = prompt.lower()
    d = {}
    if re.search(r"sin (subt|caption)", low): d["captions"] = False
    if re.search(r"sin (hook|gancho|texto)", low): d["auto_hook"] = False
    if re.search(r"sin m[uú]sica|quita.*m[uú]sica", low): d["audio"] = "strict"
    elif re.search(r"m[aá]s (sonido|audio|efectos)", low): d["audio"] = "balanced"
    d = _clean_recut(d)
    d["fallback"] = True
    return d


def _clean_recut(d: dict) -> dict:
    ok = {}
    if d.get("layout") in ("a", "b", "c"): ok["layout"] = d["layout"]
    if isinstance(d.get("hook_text"), str) and d["hook_text"].strip(): ok["hook_text"] = d["hook_text"].strip()[:60]
    for k in ("auto_hook", "captions"):
        if isinstance(d.get(k), bool): ok[k] = d[k]
    for k in ("clip_min", "clip_max"):
        if isinstance(d.get(k), (int, float)) and 5 <= d[k] <= 180: ok[k] = int(d[k])
    for k in ("trim_start", "trim_end"):
        if isinstance(d.get(k), (int, float)) and 0 < d[k] < 40: ok[k] = float(d[k])
    if d.get("audio") in ("strict", "balanced"): ok["audio"] = d["audio"]
    if isinstance(d.get("unapplied"), str) and d["unapplied"].strip(): ok["unapplied"] = d["unapplied"].strip()[:200]
    if "clip_min" in ok and "clip_max" in ok and ok["clip_max"] < ok["clip_min"] + 5:
        ok["clip_max"] = min(180, ok["clip_min"] + 5)
    return ok
