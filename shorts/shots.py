"""Footage for every text block after the hook (a new shot every 2-3 seconds) that the script
writer didn't already pick from its clip menu (shorts/menu.py): a CLIP search for the block's
"visual", preferring the hero's own video and never taking a poor match."""
from __future__ import annotations

import sqlite3

from . import db, embed, fit
from .config import Channel, Settings

FOOTAGE_WEIGHT = 0.02  # how much footage whose kind held viewers is preferred (shorts/learn.py)
SAME_VIDEO_BONUS = 0.05  # preference for staying with the hero's video (same subject, same look)
# ...but only near the hero: a long video gives several Shorts, and the rest of it belongs to them.
MIN_MATCH = 0.22  # CLIP text-image similarity below which a scene from another video isn't on topic
# (ViT-B-32: matching pairs score about 0.25-0.35, unrelated ones 0.10-0.20). Below it, the block
# stays in the hero's own video, or on the hero shot.


def assign(conn: sqlite3.Connection, settings: Settings, short_id: int,
           channel: Channel | None = None) -> None:
    """channel: footage for the blocks only comes from videos that fit it (shorts/fit.py)."""
    short = conn.execute("SELECT * FROM shorts WHERE id=?", (short_id,)).fetchone()
    hero = conn.execute("SELECT sc.*, a.duration FROM scenes sc JOIN assets a ON a.id = sc.asset_id WHERE sc.id=?",
                        (short["hero_scene_id"],)).fetchone()
    blocks = conn.execute("SELECT * FROM blocks WHERE short_id=? ORDER BY idx", (short_id,)).fetchall()
    # Footage shown on any channel stays there: the same clip on two channels looks mass-produced.
    used = db.used_scene_ids(conn, None) - {hero["id"]}
    in_short = {hero["id"]}
    hero_left = (hero["end"] - hero["start"]) - blocks[0]["seconds"]
    allowed = fit.allowed_scenes(conn, channel) if channel else None
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


def _near_hero(settings, hero) -> float:
    """How far from the hero shot its own video still belongs to this Short (seconds)."""
    from .hero import budget
    return settings.segment_seconds / 2 if budget(settings, hero["duration"]) > 1 else float("inf")


def _best(conn, settings, query, exclude, seconds, hero, allowed=None, qvec=None, held=None) -> int | None:
    half = _near_hero(settings, hero)
    hits = embed.search(conn, settings, query, k=40, exclude=exclude, allowed=allowed, qvec=qvec)
    # The hero's own video may not be in `allowed` lists or the top 40: search it on its own too.
    own = embed.search(conn, settings, query, k=10, exclude=exclude, asset_id=hero["asset_id"], qvec=qvec)
    hits = list(dict(own + hits).items())
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
        if sc["asset_id"] == hero["asset_id"]:
            if abs(sc["start"] - hero["start"]) > half:
                continue  # that part of the video belongs to its other Shorts
            score += SAME_VIDEO_BONUS
        elif score < MIN_MATCH:
            continue  # off topic: better stay in the hero's video or on the hero shot
        if held:  # footage of a kind that held viewers before, a little ahead (only with enough data)
            for key in (("source", sc["source"]), ("category", sc["category"])):
                h = held.get(key)
                if h and h["shorts"] >= 5:
                    score += FOOTAGE_WEIGHT * h["score"]
        if score > best_score:
            best, best_score = sid, score
    return best
