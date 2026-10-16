"""Runs the 'make' steps in order. Every step is resumable: it picks up Shorts by status."""
from __future__ import annotations

import logging
import sqlite3

from . import db, factcheck, hero, render, script, shots
from .config import Channel, Settings

log = logging.getLogger(__name__)


def make(conn: sqlite3.Connection, settings: Settings, channel: Channel, count: int,
         format_id: str | None = None) -> list[int]:
    fmt_id = format_id or channel.enabled_formats[0]["id"]
    from .ingest_nasa import recheck_rights
    if flagged := recheck_rights(conn):
        log.info("%d videos newly flagged for a rights check; they won't be used", flagged)

    # 2. Hero shots first; the script is written around them.
    heroes = hero.candidates(conn, settings, channel.id, count)
    if not heroes:
        log.warning("no unused hero shots left: run 'shorts ingest' to grow the library")
    else:
        log.info("picked %d hero shots", len(heroes))
        # 3. Scripts with a source per fact (batched Claude calls).
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
        factcheck.check(conn, settings, pending)

    # 4b + 5. Footage per text block, then render.
    for sid in ids("checked"):
        try:
            shots.assign(conn, settings, sid)
            out = render.render_short(conn, settings, channel, sid)
            log.info("rendered short %d -> %s", sid, out)
        except Exception as e:  # noqa: BLE001 - mark and keep going with the rest of the batch
            log.exception("short %d failed", sid)
            db.set_short(conn, sid, status="failed", error=str(e)[:1000])
