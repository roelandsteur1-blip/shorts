"""Where in its scene a b-roll block starts: the seconds that best match what the block shows.

Starting at the scene's start was the worst option in the clip-quality research (project files:
research/clip-quality.md): scenes often open on a fade, a pan or the moment before the subject
arrives. Here Perception Encoder B/16 scores a frame every half second against the block's
visual, and the block starts where a stretch of its length matches best on average (subject
clearly in view the whole time: 48% of clips at the scene start, 70% this way).

Runs on the CPU at render time, about 5 s per block; the answer is kept in blocks.clip_start
(a time in the source video), so a re-render doesn't redo it."""
from __future__ import annotations

import logging
import sqlite3

import numpy as np

from .config import Settings

log = logging.getLogger(__name__)

STEP = 0.5        # seconds between the frames that are scored
MAX_FRAMES = 40   # a long scene is sampled more thinly, not read whole
MIN_SPARE = 0.5   # less room than this in the scene: just start at its start


def best_start(times: list[float], sims: np.ndarray, end: float, seconds: float) -> float:
    """The start time whose `seconds`-long stretch of frames matches best on average."""
    starts = [i for i, t in enumerate(times) if t + seconds <= end + 0.01] or [0]
    best, best_v = starts[0], -np.inf
    for i in starts:
        inside = [j for j, t in enumerate(times) if times[i] <= t < times[i] + seconds] or [i]
        v = float(np.mean(sims[inside]))
        if v > best_v:
            best, best_v = i, v
    return times[best]


def choose(path: str, start: float, end: float, seconds: float, words: str, encoder,
           width: int, height: int) -> float:
    from .scenes import FRAME_W, _ffmpeg_frames, _frame_height
    length = end - start
    if length - seconds < MIN_SPARE or not words:
        return start
    fps = min(1 / STEP, MAX_FRAMES / length)
    frames = list(_ffmpeg_frames(path, start, length, fps, FRAME_W, _frame_height(width, height)))
    if len(frames) < 2:
        return start
    times = [start + i / fps for i in range(len(frames))]
    sims = encoder.arrays(frames, pad=False) @ encoder.texts([words])[0]
    return best_start(times, sims, end, seconds)


def is_broll(b, hero_scene_id: int | None, prev_scene_id: int | None) -> bool:
    """A block that shows a clip of its own: not the hook, not the hero shot, not a block that
    continues the shot before it (render.py carries on where that one stopped)."""
    return b["idx"] > 0 and b["scene_id"] != hero_scene_id and b["scene_id"] != prev_scene_id


def words(visual: str | None, topic: str | None) -> str:
    """What the block's clip should show: its visual, or the Short's topic (what shots.py
    searched with when the writer asked for the hero shot and it had run out)."""
    v = (visual or "").strip()
    return v if v and v.lower() != "hero" else (topic or "").strip()


def fill(conn: sqlite3.Connection, settings: Settings, short_id: int) -> int:
    """Set clip_start for every b-roll block of a Short that has none (or one from another scene).
    Returns how many were set."""
    short = conn.execute("SELECT hero_scene_id, topic FROM shorts WHERE id=?", (short_id,)).fetchone()
    blocks = conn.execute(
        """SELECT b.id, b.idx, b.seconds, b.visual, b.scene_id, b.clip_start, sc.start, sc.end,
                  a.path, a.width, a.height
             FROM blocks b JOIN scenes sc ON sc.id = b.scene_id JOIN assets a ON a.id = sc.asset_id
            WHERE b.short_id = ? ORDER BY b.idx""", (short_id,)).fetchall()
    encoder, done, prev = None, 0, None
    for b in blocks:
        broll = is_broll(b, short["hero_scene_id"] if short else None, prev)
        prev = b["scene_id"]
        if not broll:
            continue
        cs = b["clip_start"]
        if cs is not None and b["start"] <= cs <= b["end"]:
            continue  # chosen before, for this same scene
        if encoder is None:
            from . import embed
            try:
                encoder = embed.get_pe(settings)
            except Exception as e:  # noqa: BLE001 - no model: clips start at the scene start
                log.warning("Perception Encoder not available, no in-points: %s", e)
                return done
        try:
            cs = choose(b["path"], b["start"], b["end"], b["seconds"],
                        words(b["visual"], short["topic"] if short else None), encoder,
                        b["width"] or 1920, b["height"] or 1080)
        except Exception as e:  # noqa: BLE001 - the scene start is still a fine place to begin
            log.warning("short %d block %d: no in-point (%s)", short_id, b["idx"], e)
            cs = b["start"]
        conn.execute("UPDATE blocks SET clip_start=? WHERE id=?", (cs, b["id"]))
        done += 1
    conn.commit()
    return done
