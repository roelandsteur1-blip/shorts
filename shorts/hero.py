"""Pick hero shots: the opening image each Short is built around (footage first, then script)."""
from __future__ import annotations

import sqlite3

import numpy as np

from . import db, embed, sources
from .categorize import scene_filter
from .config import Settings

# What a scroll-stopping first frame looks like, as text for CLIP.
HERO_QUERY = "a stunning dramatic space image, rocket launch fire, glowing planet, explosion of light"
MIN_HERO_SECONDS = 4.0  # the hook block is usually 3-4 s; a shorter shot would freeze


HERO_SIMILARITY = 0.90  # a second hero from one video must look less alike than this (CLIP cosine)


def budget(settings: Settings, duration: float | None) -> int:
    """How many Shorts one video may open: one per segment_seconds of footage, at least one,
    at most max_shorts_per_video. A 2-minute clip gives 1 Short, a 10-minute broadcast up to 5."""
    segments = int((duration or 0) // max(settings.segment_seconds, 1))
    return max(1, min(settings.max_shorts_per_video, segments))


def earlier_heroes(conn: sqlite3.Connection, channel: str | None = None) -> dict[int, list[sqlite3.Row]]:
    """Hero scenes of non-rejected Shorts per video (on one channel, or on all when None)."""
    rows = conn.execute(
        f"""SELECT sc.asset_id, sc.start, sc.embedding FROM shorts s JOIN scenes sc ON sc.id = s.hero_scene_id
             WHERE s.status != 'rejected' {"AND s.channel = ?" if channel else ""}""",
        (channel,) if channel else (),
    ).fetchall()
    out: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        out.setdefault(r["asset_id"], []).append(r)
    return out


def far_enough(settings: Settings, start: float, others: list[float]) -> bool:
    return all(abs(start - o) >= settings.segment_seconds for o in others)


def _eligible(conn: sqlite3.Connection, settings: Settings, channel: str, use_clip: bool,
              categories: list[str] | None) -> list[sqlite3.Row]:
    """Scenes that could open the next Short: long enough, rights clear, not stock B-roll, not used
    yet, from a video with budget left, and at least segment_seconds away from (and, with CLIP,
    not looking like) the heroes that video already gave."""
    cond, args = scene_filter(categories)
    stock = sources.stock_sources()
    rows = conn.execute(
        f"""SELECT sc.id, sc.asset_id, sc.motion, sc.contrast, sc.start, sc.end, a.duration,
                   {"sc.embedding" if use_clip else "NULL AS embedding"}
              FROM scenes sc JOIN assets a ON a.id = sc.asset_id
             WHERE sc.rejected = 0 AND a.needs_check = 0 AND (sc.end - sc.start) >= ?
               AND a.source NOT IN ({",".join("?" * len(stock))}) AND {cond}""",
        (MIN_HERO_SECONDS, *stock, *args),
    ).fetchall()
    earlier = earlier_heroes(conn, channel)
    used_scenes = db.used_scene_ids(conn, channel)  # also scenes already shown as B-roll
    out = []
    for r in rows:
        prev = earlier.get(r["asset_id"], [])
        if r["id"] in used_scenes or len(prev) >= budget(settings, r["duration"]):
            continue
        if not far_enough(settings, r["start"], [p["start"] for p in prev]):
            continue
        if use_clip and r["embedding"] is not None and any(
                p["embedding"] is not None and float(np.frombuffer(r["embedding"], dtype=np.float32)
                                                     @ np.frombuffer(p["embedding"], dtype=np.float32))
                >= HERO_SIMILARITY for p in prev):
            continue
        out.append(r)
    return out


def candidates(conn: sqlite3.Connection, settings: Settings, channel: str, n: int,
               use_clip: bool = True, categories: list[str] | None = None,
               query: str | None = None) -> list[int]:
    """Return up to n hero scene ids, at most one per source video in one batch (siblings from a
    long video come in later batches, so their script knows what the earlier Shorts said).
    A video can open several Shorts over time, see budget(). Footage flagged for a rights check is
    skipped, and so is stock B-roll (Pexels etc.: fine between blocks, no story to tell as a hero).
    categories: only scenes sorted into one of these (the channel's); none = any scene."""
    rows = _eligible(conn, settings, channel, use_clip, categories)
    # Scenes not embedded yet (run 'embed') would switch CLIP scoring off for all: leave them out.
    if use_clip and any(r["embedding"] is not None for r in rows):
        rows = [r for r in rows if r["embedding"] is not None]
    if not rows:
        return []

    motion = np.array([r["motion"] or 0 for r in rows])
    contrast = np.array([r["contrast"] or 0 for r in rows])
    # Movement helps up to a point; shaky/flashing footage scores no higher.
    score = 0.45 * np.clip(motion / 0.05, 0, 1) + 0.30 * np.clip(contrast / 0.25, 0, 1)

    if use_clip and rows[0]["embedding"] is not None:
        q = embed.get_encoder(settings).texts([query or HERO_QUERY])[0]
        mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        sims = mat @ q
        span = sims.max() - sims.min()
        score = score + 0.25 * ((sims - sims.min()) / span if span > 0 else 0)

    score = score + learned_bonus(conn, channel, [r["asset_id"] for r in rows])

    picked, seen_assets = [], set()
    for i in np.argsort(-score):
        r = rows[int(i)]
        if r["asset_id"] in seen_assets:
            continue
        picked.append(int(r["id"]))
        seen_assets.add(r["asset_id"])
        if len(picked) >= n:
            break
    return picked


def per_video(conn: sqlite3.Connection, settings: Settings, channel: str,
              categories: list[str] | None = None) -> list[dict]:
    """Per video that can still open a Short on this channel: how many more (an estimate: as many
    heroes as its budget allows that also lie segment_seconds apart; the look-alike check is left
    out, it needs CLIP), how many it gave already, and its budget. Most Shorts left first."""
    by_asset: dict[int, list[sqlite3.Row]] = {}
    for r in _eligible(conn, settings, channel, False, categories):
        by_asset.setdefault(r["asset_id"], []).append(r)
    earlier = earlier_heroes(conn, channel)
    out = []
    for aid, rows in by_asset.items():
        starts = [p["start"] for p in earlier.get(aid, [])]
        made, cap = len(starts), budget(settings, rows[0]["duration"])
        for r in sorted(rows, key=lambda r: r["start"]):
            if len(starts) >= cap:
                break
            if far_enough(settings, r["start"], starts):
                starts.append(r["start"])
        title = conn.execute("SELECT title FROM assets WHERE id=?", (aid,)).fetchone()[0]
        out.append({"asset_id": aid, "title": title, "duration": rows[0]["duration"],
                    "made": made, "budget": cap, "left": len(starts) - made})
    return sorted(out, key=lambda v: -v["left"])


def shorts_left(conn: sqlite3.Connection, settings: Settings, channel: str,
                categories: list[str] | None = None) -> tuple[int, int]:
    """(Shorts the library can still open on this channel, videos they come from)."""
    videos = per_video(conn, settings, channel, categories)
    return sum(v["left"] for v in videos), len(videos)


def learned_bonus(conn: sqlite3.Connection, channel: str, asset_ids: list[int]) -> np.ndarray:
    """Feedback from analytics (step 8): NASA keywords whose Shorts kept viewers watching get a
    small bonus. Does nothing until there are published Shorts with stats."""
    rows = conn.execute(
        """SELECT a.keywords, MAX(st.engaged_views * 1.0 / NULLIF(st.views, 0)) AS ratio
             FROM shorts s JOIN scenes sc ON sc.id = s.hero_scene_id JOIN assets a ON a.id = sc.asset_id
             JOIN stats st ON st.short_id = s.id
            WHERE s.channel = ? AND st.views >= 200 GROUP BY s.id""",
        (channel,),
    ).fetchall()
    if len(rows) < 10:
        return np.zeros(len(asset_ids))
    mean = np.mean([r["ratio"] for r in rows if r["ratio"] is not None] or [0])
    kw_score: dict[str, list[float]] = {}
    for r in rows:
        for kw in (r["keywords"] or "").lower().split(","):
            kw = kw.strip()
            if kw and r["ratio"] is not None:
                kw_score.setdefault(kw, []).append(r["ratio"] - mean)
    kw_mean = {k: float(np.mean(v)) for k, v in kw_score.items() if len(v) >= 2}
    kws = {aid: (conn.execute("SELECT keywords FROM assets WHERE id=?", (aid,)).fetchone()[0] or "")
           for aid in set(asset_ids)}
    out = []
    for aid in asset_ids:
        vals = [kw_mean[k.strip()] for k in kws[aid].lower().split(",") if k.strip() in kw_mean]
        out.append(float(np.clip(np.mean(vals) if vals else 0.0, -0.15, 0.15)))
    return np.array(out)
