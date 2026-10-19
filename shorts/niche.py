"""Niche screen: before any script is written, one cheap Claude call checks a batch of candidate
videos (title, subjects, description) against the channel's niche. Footage categories are broad
(a "nature" or "history" video can be a flood newsreel), and a script call that ends in "doesn't
fit the niche" costs a full prompt per video. Answers are remembered per channel: misses go into
off_niche (never offered again), passes into niche_ok (not asked again until the niche changes)."""
from __future__ import annotations

import logging
import sqlite3

from . import hero, llm
from .config import Channel, Settings

log = logging.getLogger(__name__)

BATCH = 40          # videos per screening call
POOL = 3            # candidates screened per hero wanted (the top ones by hero score)
MIN_POOL = 12
ROUNDS = 3          # rescreens when too many of the pool were off-niche

PROMPT = """You screen archive videos for the YouTube Shorts channel "{name}". The channel is only about: {niche}.

For each video, say whether a Short about it would clearly be about that niche. A loose link is not
enough (a flood newsreel is not medicine; a steel mill is not biology). When the title, subjects and
description don't say what the footage shows, answer false.

Videos:
{items}

Answer with only JSON: [{{"n": <video number>, "fits": true or false, "why": "a few words"}}]
"""


def unscreened(conn: sqlite3.Connection, channel: Channel, asset_ids: list[int]) -> list[int]:
    ok = {r[0] for r in conn.execute("SELECT asset_id FROM niche_ok WHERE channel=? AND niche=?",
                                     (channel.id, channel.niche))}
    return [a for a in dict.fromkeys(asset_ids) if a not in ok]


def screen(conn: sqlite3.Connection, settings: Settings, channel: Channel, asset_ids: list[int]) -> int:
    """Ask Claude about these videos and remember the answers. Returns how many were off-niche.
    A failed call leaves them unscreened: the script writer's own niche check still catches them."""
    from .script import mark_off_niche
    off = 0
    for i in range(0, len(asset_ids), BATCH):
        ids = asset_ids[i:i + BATCH]
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT id, title, keywords, description FROM assets WHERE id IN ({','.join('?' * len(ids))})", ids)}
        batch = [rows[a] for a in ids if a in rows]
        items = "\n".join(
            f"{n + 1}. {a['title'] or ''} | subjects: {(a['keywords'] or '')[:200]} | "
            f"{(a['description'] or '')[:300]}" for n, a in enumerate(batch))
        try:
            reply = llm.ask_json(PROMPT.format(name=channel.name, niche=channel.niche, items=items),
                                 settings.claude_cmd, conn=conn, stage="claude niche screen")
        except llm.LLMError as e:
            log.warning("%s: niche screen failed (%s); the script writer checks these videos instead",
                        channel.id, e)
            return off
        for r in reply if isinstance(reply, list) else []:
            try:
                n, fits = int(r.get("n")), r.get("fits")
            except (TypeError, ValueError, AttributeError):
                continue
            if not 1 <= n <= len(batch) or not isinstance(fits, bool):
                continue
            aid = batch[n - 1]["id"]
            if fits:
                conn.execute("INSERT OR REPLACE INTO niche_ok (channel, asset_id, niche) VALUES (?,?,?)",
                             (channel.id, aid, channel.niche))
            else:
                mark_off_niche(conn, channel, aid, str(r.get("why") or "off-niche (screen)"))
                off += 1
        conn.commit()
    return off


def pick_heroes(conn: sqlite3.Connection, settings: Settings, channel: Channel, n: int) -> list[int]:
    """hero.candidates(), with the top of the list screened for the niche first: up to n hero scene
    ids from videos that fit (or that the screen couldn't judge)."""
    picked: list[int] = []
    for _ in range(ROUNDS):
        pool = hero.candidates(conn, settings, channel.id, max(n * POOL, MIN_POOL),
                               categories=channel.categories, query=channel.hero_query)
        if not pool:
            return []
        asset = {sid: conn.execute("SELECT asset_id FROM scenes WHERE id=?", (sid,)).fetchone()[0]
                 for sid in pool}
        todo = unscreened(conn, channel, list(asset.values()))
        if todo and (off := screen(conn, settings, channel, todo)):
            log.info("%s: niche screen: %d of %d new videos don't fit", channel.id, off, len(todo))
        blocked = {r[0] for r in conn.execute("SELECT asset_id FROM off_niche WHERE channel=?", (channel.id,))}
        picked = [sid for sid in pool if asset[sid] not in blocked][:n]
        # Enough fits, or nothing new left to screen in this pool: done.
        if len(picked) >= n or not todo or len(pool) < max(n * POOL, MIN_POOL):
            return picked
    return picked
