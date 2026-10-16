"""Pick footage for every text block after the hook (a new shot every 2-3 seconds)."""
from __future__ import annotations

import sqlite3

from . import db, embed
from .config import Settings

SAME_VIDEO_BONUS = 0.03  # mild preference for staying with the hero's video (visual coherence)


def assign(conn: sqlite3.Connection, settings: Settings, short_id: int) -> None:
    short = conn.execute("SELECT * FROM shorts WHERE id=?", (short_id,)).fetchone()
    hero = conn.execute("SELECT * FROM scenes WHERE id=?", (short["hero_scene_id"],)).fetchone()
    blocks = conn.execute("SELECT * FROM blocks WHERE short_id=? ORDER BY idx", (short_id,)).fetchall()
    used = db.used_scene_ids(conn, short["channel"]) - {hero["id"]}
    in_short = {hero["id"]}
    hero_left = (hero["end"] - hero["start"]) - blocks[0]["seconds"]

    for b in blocks[1:]:
        if b["scene_id"]:
            in_short.add(b["scene_id"])
            continue
        if b["visual"] in (None, "", "hero") and hero_left >= b["seconds"]:
            # Continue on the hero shot (render picks up where the previous block stopped).
            conn.execute("UPDATE blocks SET scene_id=? WHERE id=?", (hero["id"], b["id"]))
            hero_left -= b["seconds"]
            continue
        query = f"{b['visual'] or ''}, {short['topic'] or ''}".strip(", ")
        best = _best(conn, settings, query, used | in_short, b["seconds"], hero["asset_id"])
        if best is None:
            best = hero["id"]
        conn.execute("UPDATE blocks SET scene_id=? WHERE id=?", (best, b["id"]))
        in_short.add(best)
    conn.commit()


def _best(conn, settings, query, exclude, seconds, hero_asset) -> int | None:
    hits = embed.search(conn, settings, query, k=40, exclude=exclude)
    best, best_score = None, -1e9
    for sid, score in hits:
        sc = conn.execute(
            """SELECT sc.start, sc.end, sc.asset_id, a.needs_check FROM scenes sc
                 JOIN assets a ON a.id = sc.asset_id WHERE sc.id=?""", (sid,)).fetchone()
        if sc["needs_check"] or sc["end"] - sc["start"] < seconds:
            continue
        if sc["asset_id"] == hero_asset:
            score += SAME_VIDEO_BONUS
        if score > best_score:
            best, best_score = sid, score
    return best
