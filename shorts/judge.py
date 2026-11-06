"""Footage judges: does a clip really show the subject, and is it interesting to watch?
(research/footage-filter-models.md, 6 November 2026)

Two judges answer the same questions about a set of clips of ONE subject:
- Gemini (Google API, free tier) watches the clip itself, motion included. Arena's Entity
  Recognition board (19 Oct 2026) has Gemini at places 1-5, so it decides identity.
- Claude (the Pro plan, `claude -p`) sees 5 sharp frames per clip. It is the second opinion on
  identity and judges the rest as well.

Every verdict has: shows (bool), confidence 1-5, why (one line), same_shot (no cut / title card),
era_ok (old film where the niche needs it) and five interest parts 1-5: action, subject, wow,
viral (would people rewatch/comment/share it), vintage (old footage scores higher: Roeland,
6 Nov), looks. Photos (slow zoom) score action <= 2, and the collector prefers video anyway.

`settings.footage_judge` picks what the collector uses:
  "claude"  - today's blind check (3 frames, yes/no); no interest score
  "claude+" - Claude only, with the stronger questions and the interest score
  "gemini"  - Gemini only
  "both"    - Gemini decides identity, Claude must not say no; interest averaged
Without a Gemini key "gemini"/"both" fall back to "claude+".
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from . import llm

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-3-flash"       # matched against the API's model list. 3.8 Flash: free tier overloaded
                                      # (503 / no answer in 10 min, geminiprobe-1, 6 Nov); 3 Flash: 10 clips in 15 s
GEMINI_THINKING = "medium"            # thinkingLevel (3.8 Flash default); Roeland: low may judge "viral" worse
GEMINI_BATCH = 10                     # clips per Gemini request (3 Flash: 10 clips in 15 s; fewer requests = free quota)
WEIGHTS = {"action": 0.25, "subject": 0.15, "wow": 0.15, "viral": 0.20, "vintage": 0.15, "looks": 0.10}
MIN_INTEREST = 2.5                    # clips under this are not used
MIN_CONFIDENCE = 4                    # "shows" counts only from this confidence up
OLD_NICHES = {"oldcity", "history", "retrotech"}
CLIP_SECONDS = 8.0                    # the part of a clip the judges see (what a Short shows)
FRAMES = 5
FRAME_WIDTH = 768

QUESTIONS = """For EACH clip answer:
- "shows": true only when {subject_rule}
- "confidence": 1-5, how sure you are about "shows" (5 = certain).
- "why": max 12 words: what you see that decides it (e.g. "elliptical wings and roundels: a Spitfire").
- "same_shot": true unless the clip cuts to a different scene, a title card, or a presenter/diagram
  replacing the subject. Camera moves, zooms and the subject moving are fine.
- "era_ok": {era_rule}
- Interest, each 1-5 (judge as a viewer scrolling YouTube Shorts on a phone):
  - "action": does something visibly happen (launch, take-off, wave breaking, animal moving)? 1 = static.
  - "subject": one clear subject, big enough to see on a phone? 1 = tiny, lost or cluttered.
  - "wow": would a viewer stop scrolling: unusual angle, rare moment, scale, close-up? 1 = dull.
  - "viral": viral potential: would this moment make people rewatch, comment or share (jaw-dropping,
    "how is that real", satisfying, dramatic, funny, record-breaking)? 1 = nobody would share it.
  - "vintage": how old it looks: our viewers find old footage more interesting. 5 = clearly old archive
    film (black and white, early colour, film grain), 3 = 1970s-90s look, 1 = clearly modern digital footage.
  - "looks": clean picture, not washed out, blurry, tiny or shaky; works cropped to a vertical frame?
Some clips are a still PHOTO with an added slow zoom: real video is worth more to us, so for a photo
"action" is at most 2.
Be strict and use the whole 1-5 range: most archive clips are a 2 or 3 on "wow" and "viral".

Return JSON only: {{"clips": [{{"label": "A", "shows": true, "confidence": 5, "why": "...", "same_shot": true,
"era_ok": true, "action": 3, "subject": 4, "wow": 2, "viral": 2, "vintage": 4, "looks": 3}}]}}"""

SUBJECT_RULE = ('the named subject "{name}" is clearly and unmistakably identifiable (a different '
                'building, animal, plane or object that merely looks similar is false; ignore any title '
                'you may know, judge only what is visible).')
ERA_OLD = ("true when it looks like old film or old photography (black and white, early colour, film "
           "grain, period details); false when it is clearly modern footage.")
ERA_ANY = "always true for this channel."

GEMINI_PROMPT = """You check archive video clips for a YouTube Shorts channel about {niche}.
Every clip below should show: {name}{show}. The clips are labelled in order: {labels}.
Watch each whole clip, including what moves and any cuts.

""" + QUESTIONS

CLAUDE_PROMPT = """You check archive video clips for a YouTube Shorts channel about {niche}.
Every clip below should show: {name}{show}. Each clip is {frames} frames spread over its {seconds:.0f} seconds,
in order; the images come in this order: {order}.
You only see frames, so judge "action" from how much changes between them.

""" + QUESTIONS


class JudgeError(RuntimeError):
    pass


# ---------- shared ----------

def interest(v: dict) -> float:
    """Weighted interest 1-5 of one verdict."""
    total = 0.0
    for k, w in WEIGHTS.items():
        try:
            x = float(v.get(k) or 0)
        except (TypeError, ValueError):
            x = 0.0
        total += w * min(5.0, max(1.0, x)) if x else w * 1.0
    return round(total, 2)


def _clean(raw: dict) -> dict:
    def num(k):
        try:
            return max(1, min(5, int(round(float(raw.get(k))))))
        except (TypeError, ValueError):
            return None
    v = {"shows": raw.get("shows") is True, "confidence": num("confidence") or 1,
         "why": str(raw.get("why") or "")[:160], "same_shot": raw.get("same_shot") is not False,
         "era_ok": raw.get("era_ok") is not False}
    for k in WEIGHTS:
        v[k] = num(k) or 1
    v["interest"] = interest(v)
    return v


def passes(v: dict | None) -> bool:
    return bool(v) and v["shows"] and v["confidence"] >= MIN_CONFIDENCE and v["same_shot"] and v["era_ok"]


def _rules(niche: str, name: str) -> tuple[str, str]:
    return SUBJECT_RULE.format(name=name), (ERA_OLD if niche in OLD_NICHES else ERA_ANY)


def _parse(text: str, labels: list[str]) -> dict[str, dict]:
    data = llm.extract_json(text) if isinstance(text, str) else text
    out = {}
    for c in (data or {}).get("clips", []):
        lab = str(c.get("label") or "")
        if lab in labels:
            out[lab] = _clean(c)
    return out


def excerpt(path: str, start: float = 0.0, seconds: float = CLIP_SECONDS, height: int = 360) -> bytes:
    """A small mp4 of the part we'd show (no sound), for Gemini."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "x.mp4"
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, start):.2f}",
                            "-i", path, "-t", f"{seconds:.2f}", "-an", "-vf", f"scale=-2:{height}",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
                            str(out)], capture_output=True, timeout=180)
        if p.returncode or not out.exists():
            raise JudgeError("excerpt failed: " + p.stderr.decode(errors="replace")[-200:])
        return out.read_bytes()


def frames(path: str, start: float = 0.0, seconds: float = CLIP_SECONDS, n: int = FRAMES,
           width: int = FRAME_WIDTH) -> list[bytes]:
    """n JPEG frames spread over the part we'd show."""
    out = []
    for i in range(n):
        t = start + 0.3 + (seconds - 0.6) * i / max(1, n - 1)
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path,
                            "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg",
                            "-q:v", "4", "-"], capture_output=True, timeout=60)
        if p.stdout:
            out.append(p.stdout)
    return out


# ---------- Gemini ----------

_models: dict[str, str] = {}


def gemini_model(key: str, wanted: str = GEMINI_MODEL) -> str:
    """The API's id for `wanted` ("gemini-3.8-flash" may be listed as "...-preview-10-2026"): exact
    name first, else the newest-sounding name that starts with it."""
    if wanted in _models:
        return _models[wanted]
    r = requests.get(f"{API}/models", params={"key": key, "pageSize": 1000}, timeout=30)
    if r.status_code != 200:
        raise JudgeError(f"Gemini model list: HTTP {r.status_code} {r.text[:200]}")
    names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    pick = wanted if wanted in names else next(
        (n for n in sorted(names, reverse=True) if n.startswith(wanted) and "image" not in n and "tts" not in n), None)
    if not pick:
        raise JudgeError(f"Gemini has no model like {wanted}; it lists: {', '.join(names[:40])}")
    _models[wanted] = pick
    return pick


def gemini_config(thinking: str | None = GEMINI_THINKING) -> dict:
    """No temperature: Gemini 3 models are tuned for the default 1.0 and can loop below it.
    Thinking 'medium' (3.8 Flash's default): kept for the judgement calls (viral, wow); requests
    carry GEMINI_BATCH clips so it finishes in time."""
    cfg: dict = {"responseMimeType": "application/json"}
    if thinking:
        cfg["thinkingConfig"] = {"thinkingLevel": thinking}
    return cfg


def gemini_call(key: str, model: str, parts: list[dict], tries: int = 6, config: dict | None = None) -> str:
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": config or gemini_config()}
    wait = 20.0
    for attempt in range(tries):
        try:
            r = requests.post(f"{API}/models/{model}:generateContent", headers={"x-goog-api-key": key},
                              json=body, timeout=300)
        except requests.Timeout:
            if attempt >= 1:
                raise JudgeError("Gemini timed out twice")
            log.info("Gemini timed out, trying once more")
            continue
        if r.status_code == 200:
            data = r.json()
            try:
                return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            except (KeyError, IndexError) as e:
                raise JudgeError(f"Gemini gave no answer: {json.dumps(data)[:300]}") from e
        if r.status_code in (429, 500, 503) and attempt < tries - 1:
            # free tier: per-minute limits; the error says how long to wait
            delay = wait
            try:
                for d in r.json().get("error", {}).get("details", []):
                    if str(d.get("@type", "")).endswith("RetryInfo"):
                        delay = float(str(d.get("retryDelay", "0s")).rstrip("s") or 0) + 2
            except ValueError:
                pass
            if r.status_code == 429:
                log.warning("Gemini quota: %s", r.text[:1500])
                if "PerDay" in r.text:
                    raise JudgeError("Gemini daily free limit reached: " + r.text[:600])
            log.info("Gemini HTTP %s, waiting %.0f s", r.status_code, delay)
            time.sleep(min(delay, 120))
            wait *= 1.5
            continue
        if r.status_code == 400 and "thinking" in r.text.lower() and "thinkingConfig" in body["generationConfig"]:
            body["generationConfig"] = {k: v for k, v in body["generationConfig"].items() if k != "thinkingConfig"}
            log.info("Gemini %s doesn't take this thinking level: its default instead", model)
            continue
        raise JudgeError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
    raise JudgeError("Gemini: too many retries")


def gemini_judge(key: str, clips: list[dict], name: str, niche: str, show: str = "",
                 model: str = GEMINI_MODEL, thinking: str | None = GEMINI_THINKING) -> dict[str, dict]:
    """clips: [{label, path, start?, seconds?}] -> {label: verdict}. GEMINI_BATCH clips per
    request, the requests at the same time."""
    from concurrent.futures import ThreadPoolExecutor
    mid = gemini_model(key, model)
    batches = [clips[i:i + GEMINI_BATCH] for i in range(0, len(clips), GEMINI_BATCH)]
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(batches) or 1) as pool:
        for got in pool.map(lambda b: _gemini_batch(key, mid, b, name, niche, show, thinking), batches):
            out.update(got)
    return out


def _gemini_batch(key: str, mid: str, clips: list[dict], name: str, niche: str, show: str,
                  thinking: str | None) -> dict[str, dict]:
    subj, era = _rules(niche, name)
    labels = [c["label"] for c in clips]
    parts: list[dict] = [{"text": GEMINI_PROMPT.format(niche=niche, name=name, show=f" ({show})" if show else "",
                                                       labels=", ".join(labels), subject_rule=subj, era_rule=era)}]
    for c in clips:
        data = excerpt(c["path"], c.get("start", 0.0), c.get("seconds", CLIP_SECONDS))
        parts += [{"text": f"Clip {c['label']}:"},
                  {"inline_data": {"mime_type": "video/mp4", "data": base64.b64encode(data).decode("ascii")}}]
    return _parse(gemini_call(key, mid, parts, config=gemini_config(thinking)), labels)


# ---------- Claude ----------

def claude_judge(settings, clips: list[dict], name: str, niche: str, show: str = "",
                 model: str = "") -> dict[str, dict]:
    """Same questions, 5 frames of 768 px per clip as separate images."""
    subj, era = _rules(niche, name)
    images, order = [], []
    for c in clips:
        fr = frames(c["path"], c.get("start", 0.0), c.get("seconds", CLIP_SECONDS))
        if not fr:
            continue
        order.append(f"clip {c['label']} = images {len(images) + 1}-{len(images) + len(fr)}")
        images += fr
    if not images:
        return {}
    labels = [c["label"] for c in clips]
    prompt = CLAUDE_PROMPT.format(niche=niche, name=name, show=f" ({show})" if show else "", frames=FRAMES,
                                  seconds=clips[0].get("seconds", CLIP_SECONDS), order="; ".join(order),
                                  subject_rule=subj, era_rule=era)
    model = model or getattr(settings, "judge_claude_model", "") or ""
    return _parse(llm.ask_json(prompt, settings.claude_cmd, retries=1, images=images, model=model), labels)


# ---------- the collector's view ----------

def combine(g: dict | None, c: dict | None) -> dict | None:
    """Gemini decides identity; Claude may veto with a confident no. Interest: average, Gemini's
    action counts double (only Gemini sees motion)."""
    if not g and not c:
        return None
    if not g or not c:
        return dict(g or c)
    v = dict(g)
    v["shows"] = g["shows"] and not (c["shows"] is False and c["confidence"] >= MIN_CONFIDENCE)
    v["same_shot"] = g["same_shot"] and c["same_shot"]
    v["era_ok"] = g["era_ok"] and c["era_ok"]
    for k in WEIGHTS:
        v[k] = round((2 * g[k] + c[k]) / 3, 2) if k == "action" else round((g[k] + c[k]) / 2, 2)
    v["interest"] = interest(v)
    v["why"] = f"Gemini: {g['why']} | Claude: {c['why']}"
    return v


def mode(settings) -> str:
    from .sources import api_key
    m = getattr(settings, "footage_judge", "claude") or "claude"
    if m in ("gemini", "both") and not api_key(settings, "gemini"):
        return "claude+"
    return m


def judge(settings, clips: list[dict], name: str, niche: str, show: str = "") -> dict[str, dict]:
    """{label: verdict} by the configured judge(s). A failing Gemini call falls back to Claude."""
    from .sources import api_key
    m = mode(settings)
    g = c = {}
    if m in ("gemini", "both"):
        try:
            g = gemini_judge(api_key(settings, "gemini"), clips, name, niche, show,
                             model=getattr(settings, "gemini_model", GEMINI_MODEL) or GEMINI_MODEL)
        except (JudgeError, requests.RequestException) as e:
            log.warning("Gemini judge failed (%s); Claude judges alone", e)
            m = "claude+"
    if m in ("claude+", "both"):
        c = claude_judge(settings, clips, name, niche, show)
    labels = [x["label"] for x in clips]
    if m == "gemini":
        return {k: v for k, v in g.items() if k in labels}
    if m == "claude+":
        return c
    return {lab: v for lab in labels if (v := combine(g.get(lab), c.get(lab)))}
