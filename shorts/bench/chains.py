"""Step 4 of the research: the same moments shown three ways, for a blind review by a person.

    today   what the Short shows now: the pipeline's own pick, from the start of the scene,
            cropped by framing.py with the CLIP text guide
    winner  the bench's winners: PE B/16 on 3 frames (+ title words) picks the clip, it starts at
            the seconds that best match the writer's request, YOLOE aims the crop (saliency when
            it finds nothing)
    cheap   the winner's clip, its middle seconds, saliency crop (no detector)

Each becomes a short silent 9:16 video, cut and cropped the way render.py does it. The
dashboard's /bench/review shows the three of a moment side by side in a shuffled order, without
names, and the person picks the best one."""
from __future__ import annotations

import logging
import random
import subprocess
import time

import numpy as np

from .. import db, fit, framing, render
from ..config import load_channel
from . import SEED, load, root, save, snapshot, status_line
from .clips import Padded, _pick
from .detect import box_to_crop, load_finder
from .frames import dense
from .models import Encoder

log = logging.getLogger(__name__)

CHAINS = ("today", "winner", "cheap")
SEARCH = "pe-b16/max3+meta"   # the bench's best way of finding the clip
MATCH = "pe-b16"              # the model that finds the best seconds
TODAY = "clip-b32"            # today's model, for today's text-guided crop
FINDER = "yoloe"
MOMENTS = 20
STEP = 0.5
OUT_W, OUT_H = 540, 960


def path(settings):
    return root(settings) / "chains.json"


def clip_file(settings, block: str | int, chain: str):
    return root(settings) / "chains" / f"{block}-{chain}.mp4"


def letters(block: str | int) -> dict[str, str]:
    """The letters a person sees for one moment, in an order fixed per moment but random."""
    order = list(CHAINS)
    random.Random(f"{SEED}-{block}").shuffle(order)
    return {chr(65 + i): c for i, c in enumerate(order)}


def _winner_scene(b: dict, ts: dict, ranks: dict, search: str, exclude: set[int],
                  allowed: set[int] | None) -> str | None:
    """The search's best clip that the pipeline could really use: long enough for the block, not
    shown in another Short, from a video that fits the channel."""
    for sid, _ in ranks.get(search, {}).get(str(b["id"]), []):
        s = ts["scenes"].get(str(sid))
        if not s or sid in exclude or (allowed is not None and sid not in allowed):
            continue
        if s["end"] - s["start"] >= (b["seconds"] or 2.5):
            return str(sid)
    return None


def _allowed(conn, channel: str, cache: dict) -> set[int] | None:
    if channel not in cache:
        try:
            cache[channel] = fit.allowed_scenes(conn, load_channel(channel))
        except Exception as e:  # noqa: BLE001 - no channel config here: don't filter
            log.warning("channel %s: no fit filter (%s)", channel, e)
            cache[channel] = None
    return cache[channel]


def _crop(s: dict, start: float, seconds: float, query: str | None = None, encoder=None) -> dict:
    return framing.subject_path(s["path"], start, seconds, s["width"] or 1920, s["height"] or 1080,
                                query=query, encoder=encoder)


def _crop_found(s: dict, start: float, seconds: float, finder, words: str) -> dict:
    w, h = s["width"] or 1920, s["height"] or 1080
    frames = framing.read_frames(s["path"], start, seconds, w, h)
    frames = [frames[int(i)] for i in np.linspace(0, len(frames) - 1, min(3, len(frames)))] if frames else []
    box = finder.find(frames, words) if frames and finder is not None else None
    if box is None:
        return {**_crop(s, start, seconds), "found": False}
    c = box_to_crop(box, framing.window_fraction(w, h)[0])
    return {"x0": c["x"], "x1": c["x"], "y0": 0.5, "y1": 0.5, "fy": 0.5, "whole": c["whole"], "found": True}


def render_clip(s: dict, start: float, seconds: float, frame: dict, out) -> None:
    """One silent 9:16 clip, cut and cropped like render.py cuts and crops a block."""
    w, h = s["width"] or 1920, s["height"] or 1080
    avail = max(s["end"] - start, 0.1)
    need = seconds + 0.2
    mode = "blur" if frame.get("whole") else render.choose_mode(w, h)
    seg = render._segment_filter(0, seconds, frame, w, h, mode, hold=max(0.0, need - avail))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp4")
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-ss", f"{start:.3f}",
                    "-t", f"{min(need, avail):.3f}", "-i", s["path"],
                    "-filter_complex", f"{seg};[v0]scale={OUT_W}:{OUT_H}[out]", "-map", "[out]", "-an",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(tmp)], check=True, capture_output=True, timeout=600)
    tmp.replace(out)


def run(settings, ts: dict, n: int = MOMENTS, threads: int = 4, search: str = SEARCH, match: str = MATCH,
        today: str = TODAY, finder: str | None = FINDER) -> dict:
    data = load(path(settings), {})
    ranks = load(root(settings) / "rank.json", {})
    if search not in ranks:
        log.warning("no ranking %s: run the rank stage first", search)
        return data
    conn = snapshot(settings)
    used = db.used_scene_ids(conn, None)
    fits: dict = {}
    rng = random.Random(SEED)
    blocks = [b for b in ts["blocks"] if b["picked"] and str(b["picked"]) in ts["scenes"]]
    rng.shuffle(blocks)
    plans = []
    for b in blocks:
        if len(plans) >= n:
            break
        own = {r[0] for r in conn.execute("SELECT scene_id FROM blocks WHERE short_id=? AND scene_id IS NOT NULL",
                                          (b["short_id"],))}
        exclude = (used | own) - {b["picked"]}
        sid = _winner_scene(b, ts, ranks, search, exclude, _allowed(conn, b["channel"], fits))
        if sid is not None:
            plans.append((b, sid))
    todo = [(b, sid) for b, sid in plans
            if not all(clip_file(settings, b["id"], c).exists() for c in CHAINS)]
    if not todo:
        return data
    enc_match, enc_today = Encoder(match, threads), Encoder(today, threads)
    found = load_finder(finder) if finder else None
    for i, (b, sid) in enumerate(todo):
        status_line(settings, "review clips", i, len(todo), f"block {b['id']}")
        length = b["seconds"] or 2.5
        words = b["visual"] or b["topic"] or ""
        t0 = time.perf_counter()
        # today: the pipeline's own clip from its start, text-guided crop.
        s = ts["scenes"][str(b["picked"])]
        secs = min(length, s["end"] - s["start"])
        made = {"today": {"sid": b["picked"], "start": s["start"],
                          "crop": _crop(s, s["start"], secs, words, Padded(enc_today))}}
        # winner and cheap: the new search's clip.
        s = ts["scenes"][sid]
        fr = dense(s["path"], s["start"], s["end"], STEP)
        if fr:
            times = [t for t, _ in fr]
            sims = enc_match.arrays([f for _, f in fr]) @ enc_match.texts([words])[0]
            best = _pick(times, s["end"], length, sims)
        else:
            best = s["start"]
        middle = max(s["start"], (s["start"] + s["end"] - length) / 2)
        made["winner"] = {"sid": int(sid), "start": best, "crop": _crop_found(s, best, length, found, words)}
        made["cheap"] = {"sid": int(sid), "start": middle, "crop": _crop(s, middle, length)}
        for chain, m in made.items():
            sc = ts["scenes"][str(m["sid"])]
            render_clip(sc, m["start"], length, m["crop"], clip_file(settings, b["id"], chain))
        data[str(b["id"])] = {"text": b["text"], "visual": b["visual"], "topic": b["topic"],
                              "channel": b["channel"], "seconds": length,
                              "chains": made, "secs": time.perf_counter() - t0}
        save(path(settings), data)
    status_line(settings, "review clips", len(todo), len(todo))
    return data
