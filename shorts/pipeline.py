"""Runs the 'make' steps in order. Every step is resumable: it picks up Shorts by status."""
from __future__ import annotations

import logging
import sqlite3

from . import db, factcheck, hero, progress, render, script, shots
from .config import Channel, Settings

log = logging.getLogger(__name__)


def make(conn: sqlite3.Connection, settings: Settings, channel: Channel, count: int,
         format_id: str | None = None, restock: bool = True) -> list[int]:
    fmt_id = format_id or channel.enabled_formats[0]["id"]
    from .sources.common import recheck_rights
    if flagged := recheck_rights(conn):
        log.info("%d videos newly flagged for a rights check; they won't be used", flagged)
    # Scenes cut since the last run get their categories (cheap: metadata + CLIP).
    from .categorize import sort_pending
    sort_pending(conn, settings)

    # 1. Footage running low: download, cut and embed more before picking.
    if restock:
        from .library import restock as restock_library
        restock_library(conn, settings, channel, need=count)

    # 2. Hero shots first; the script is written around them.
    heroes = hero.candidates(conn, settings, channel.id, count, categories=channel.categories,
                             query=channel.hero_query)
    if not heroes:
        log.warning("no unused hero shots left: run 'shorts ingest' to grow the library")
    else:
        log.info("picked %d hero shots", len(heroes))
        # 3. Scripts with a source per fact (batched Claude calls).
        progress.report(0, len(heroes), "script")
        script.write_scripts(conn, settings, channel, fmt_id, heroes)

    finish(conn, settings, channel)
    return [r[0] for r in conn.execute(
        "SELECT id FROM shorts WHERE channel=? AND status='rendered' ORDER BY id", (channel.id,))]


def finish(conn: sqlite3.Connection, settings: Settings, channel: Channel) -> None:
    """Fact check, footage per block and render for everything that's not done yet."""
    def ids(status: str) -> list[int]:
        return [r[0] for r in conn.execute(
            "SELECT id FROM shorts WHERE channel=? AND status=? ORDER BY id", (channel.id, status))]

    # 4a. Separate fact-check call.
    pending = ids("scripted")
    if pending:
        log.info("fact checking %d scripts", len(pending))
        progress.report(0, len(pending), "fact check")
        factcheck.check(conn, settings, pending)

    # 4b + 5. Footage per text block, then render. Failed ones are retried each run.
    todo = ids("checked") + ids("failed")
    for n, sid in enumerate(todo):
        progress.report(n, len(todo), "render")
        try:
            shots.assign(conn, settings, sid, channel.categories)
            out = render.render_short(conn, settings, channel, sid)
            log.info("rendered short %d -> %s", sid, out)
        except Exception as e:  # noqa: BLE001 - mark and keep going with the rest of the batch
            log.exception("short %d failed", sid)
            db.set_short(conn, sid, status="failed", error=str(e)[:1000])
    if todo:
        progress.report(len(todo), len(todo), "render")
