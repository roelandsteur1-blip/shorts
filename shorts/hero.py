"""Pick hero shots: the opening image each Short is built around (footage first, then script)."""
from __future__ import annotations

import sqlite3

import numpy as np

from . import db, embed
from .config import Settings

# What a scroll-stopping first frame looks like, as text for CLIP.
HERO_QUERY = "a stunning dramatic space image, rocket launch fire, glowing planet, explosion of light"
MIN_HERO_SECONDS = 2.5


def candidates(conn: sqlite3.Connection, settings: Settings, channel: str, n: int,
               use_clip: bool = True) -> list[int]:
    """Return up to n hero scene ids, at most one per source video, never reusing a video
    that already opened a Short on this channel. Footage flagged for a rights check is skipped."""
    rows = conn.execute(
        """SELECT sc.id, sc.asset_id, sc.motion, sc.contrast, sc.start, sc.end, sc.embedding
             FROM scenes sc JOIN assets a ON a.id = sc.asset_id
            WHERE sc.rejected = 0 AND a.needs_check = 0 AND (sc.end - sc.start) >= ?""",
        (MIN_HERO_SECONDS,),
    ).fetchall()
    used_assets = db.used_hero_assets(conn, channel)
    rows = [r for r in rows if r["asset_id"] not in used_assets]
    if not rows:
        return []

    motion = np.array([r["motion"] or 0 for r in rows])
    contrast = np.array([r["contrast"] or 0 for r in rows])
    # Movement helps up to a point; shaky/flashing footage scores no higher.
    score = 0.45 * np.clip(motion / 0.05, 0, 1) + 0.30 * np.clip(contrast / 0.25, 0, 1)

    if use_clip and all(r["embedding"] is not None for r in rows):
        q = embed.get_encoder(settings).texts([HERO_QUERY])[0]
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
