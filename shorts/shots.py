"""Pick footage for every text block after the hook (a new shot every 2-3 seconds)."""
from __future__ import annotations

import sqlite3

from . import db, embed
from .categorize import allowed_scenes
from .config import Settings

SAME_VIDEO_BONUS = 0.03  # mild preference for staying with the hero's video (visual coherence)
# ...but only near the hero: a long video gives several Shorts, and the rest of it belongs to them.


def assign(conn: sqlite3.Connection, settings: Settings, short_id: int,
           categories: list[str] | None = None) -> None:
    """categories: the channel's; footage for the blocks only comes from scenes in them."""
    short = conn.execute("SELECT * FROM shorts WHERE id=?", (short_id,)).fetchone()
    hero = conn.execute("SELECT * FROM scenes WHERE id=?", (short["hero_scene_id"],)).fetchone()
    blocks = conn.execute("SELECT * FROM blocks WHERE short_id=? ORDER BY idx", (short_id,)).fetchall()
    used = db.used_scene_ids(conn, short["channel"]) - {hero["id"]}
    in_short = {hero["id"]}
    hero_left = (hero["end"] - hero["start"]) - blocks[0]["seconds"]
    allowed = allowed_scenes(conn, categories)
    # One CLIP call for every block that may need a search, instead of one per block.
    queries = {b["id"]: f"{b['visual'] or ''}, {short['topic'] or ''}".strip(", ") for b in blocks[1:]
               if not b["scene_id"]}
    vecs = dict(zip(queries, embed.get_encoder(settings).texts(list(queries.values())))) if queries else {}

    for b in blocks[1:]:
        if b["scene_id"]:
            in_short.add(b["scene_id"])
            continue
        if b["visual"] in (None, "", "hero") and hero_left >= b["seconds"]:
            # Continue on the hero shot (render picks up where the previous block stopped).
            conn.execute("UPDATE blocks SET scene_id=? WHERE id=?", (hero["id"], b["id"]))
            hero_left -= b["seconds"]
            continue
        best = _best(conn, settings, queries[b["id"]], used | in_short, b["seconds"], hero, allowed,
                     vecs[b["id"]])
        if best is None:
            best = hero["id"]
        conn.execute("UPDATE blocks SET scene_id=? WHERE id=?", (best, b["id"]))
        in_short.add(best)
    conn.commit()


def _best(conn, settings, query, exclude, seconds, hero, allowed=None, qvec=None) -> int | None:
    half = settings.segment_seconds / 2
    hits = embed.search(conn, settings, query, k=40, exclude=exclude, allowed=allowed, qvec=qvec)
    if not hits:
        return None
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT sc.id, sc.start, sc.end, sc.asset_id, a.needs_check FROM scenes sc
              JOIN assets a ON a.id = sc.asset_id WHERE sc.id IN ({",".join("?" * len(hits))})""",
        [sid for sid, _ in hits])}
    best, best_score = None, -1e9
    for sid, score in hits:
        sc = rows.get(sid)
        if sc is None:
            continue
        if sc["needs_check"] or sc["end"] - sc["start"] < seconds:
            continue
        if sc["asset_id"] == hero["asset_id"] and abs(sc["start"] - hero["start"]) <= half:
            score += SAME_VIDEO_BONUS
        if score > best_score:
            best, best_score = sid, score
    return best
