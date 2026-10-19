"""Pick footage for every text block after the hook (a new shot every 2-3 seconds)."""
from __future__ import annotations

import sqlite3

from . import db, embed
from .categorize import allowed_scenes
from .config import Settings

FOOTAGE_WEIGHT = 0.02  # how much footage whose kind held viewers is preferred (shorts/learn.py)
SAME_VIDEO_BONUS = 0.03  # mild preference for staying with the hero's video (visual coherence)
# ...but only near the hero: a long video gives several Shorts, and the rest of it belongs to them.


def assign(conn: sqlite3.Connection, settings: Settings, short_id: int,
           categories: list[str] | None = None) -> None:
    """categories: the channel's; footage for the blocks only comes from scenes in them."""
    short = conn.execute("SELECT * FROM shorts WHERE id=?", (short_id,)).fetchone()
    hero = conn.execute("SELECT * FROM scenes WHERE id=?", (short["hero_scene_id"],)).fetchone()
    blocks = conn.execute("SELECT * FROM blocks WHERE short_id=? ORDER BY idx", (short_id,)).fetchall()
    # Footage shown on any channel stays there: the same clip on two channels looks mass-produced.
    used = db.used_scene_ids(conn, None) - {hero["id"]}
    in_short = {hero["id"]}
    hero_left = (hero["end"] - hero["start"]) - blocks[0]["seconds"]
    allowed = allowed_scenes(conn, categories)
    # One CLIP call for every block that may need a search, instead of one per block.
    from . import learn
    held = learn.footage_scores(conn)
    queries = {b["id"]: f"{b['visual'] or ''}, {short['topic'] or ''}".strip(", ") for b in blocks[1:]
               if not b["scene_id"]}
    vecs = dict(zip(queries, embed.get_encoder(settings).texts(list(queries.values())))) if queries else {}

    for b in blocks[1:]:
        if b["scene_id"]:
            in_short.add(b["scene_id"])
            continue
        # The last block may return to the hero shot (a Short that loops back to its start), even
        # when the hook used it up: render then shows its last seconds again.
        loop_back = b["visual"] == "hero" and b["id"] == blocks[-1]["id"]
        if b["visual"] in (None, "", "hero") and (hero_left >= b["seconds"] or loop_back):
            # Continue on the hero shot (render picks up where the previous block stopped).
            conn.execute("UPDATE blocks SET scene_id=? WHERE id=?", (hero["id"], b["id"]))
            hero_left -= b["seconds"]
            continue
        best = _best(conn, settings, queries[b["id"]], used | in_short, b["seconds"], hero, allowed,
                     vecs[b["id"]], held)
        if best is None:
            best = hero["id"]
        conn.execute("UPDATE blocks SET scene_id=? WHERE id=?", (best, b["id"]))
        in_short.add(best)
    conn.commit()


def _best(conn, settings, query, exclude, seconds, hero, allowed=None, qvec=None, held=None) -> int | None:
    half = settings.segment_seconds / 2
    hits = embed.search(conn, settings, query, k=40, exclude=exclude, allowed=allowed, qvec=qvec)
    if not hits:
        return None
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT sc.id, sc.start, sc.end, sc.asset_id, a.needs_check, a.claimed, a.source,
                   (SELECT category FROM scene_categories c WHERE c.scene_id = sc.id AND c.confidence > 0
                     ORDER BY c.confidence DESC LIMIT 1) AS category FROM scenes sc
              JOIN assets a ON a.id = sc.asset_id WHERE sc.id IN ({",".join("?" * len(hits))})""",
        [sid for sid, _ in hits])}
    best, best_score = None, -1e9
    for sid, score in hits:
        sc = rows.get(sid)
        if sc is None:
            continue
        if sc["needs_check"] or sc["claimed"] or sc["end"] - sc["start"] < seconds:
            continue
        if sc["asset_id"] == hero["asset_id"] and abs(sc["start"] - hero["start"]) <= half:
            score += SAME_VIDEO_BONUS
        if held:  # footage of a kind that held viewers before, a little ahead (only with enough data)
            for key in (("source", sc["source"]), ("category", sc["category"])):
                h = held.get(key)
                if h and h["shorts"] >= 5:
                    score += FOOTAGE_WEIGHT * h["score"]
        if score > best_score:
            best, best_score = sid, score
    return best
