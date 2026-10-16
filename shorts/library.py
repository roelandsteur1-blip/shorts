"""Keep the footage library stocked and tidy: move it to another drive, download more when it runs
low, and delete videos that have nothing left to give."""
from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from . import hero
from .config import Channel, Settings

log = logging.getLogger(__name__)

# Shorts in these states no longer need their source footage (rerender is refused once uploaded).
DONE_STATES = ("scheduled", "uploaded", "published", "rejected")


def move_footage(conn: sqlite3.Connection, settings: Settings) -> tuple[int, float]:
    """Move every downloaded video that is not under settings.footage_dir into it (same
    sub-folders) and update its path. Returns (files moved, GB moved)."""
    target = settings.footage_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)
    old_root = (settings.data_dir / "footage").resolve()
    moved, size = 0, 0.0
    for r in conn.execute("SELECT id, path FROM assets WHERE path IS NOT NULL").fetchall():
        src = Path(r["path"])
        if not src.exists():
            continue
        try:
            src.resolve().relative_to(target)
            continue  # already there
        except ValueError:
            pass
        try:
            rel = src.resolve().relative_to(old_root)
        except ValueError:
            rel = Path(src.parent.name) / src.name
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        gb = src.stat().st_size / 1e9
        shutil.move(str(src), str(dst))
        conn.execute("UPDATE assets SET path=? WHERE id=?", (str(dst), r["id"]))
        conn.commit()  # per file, so an interrupted move leaves the database right
        moved += 1
        size += gb
        log.info("moved %s -> %s (%.2f GB)", src, dst, gb)
    return moved, size


def unused_hero_videos(conn: sqlite3.Connection, settings: Settings, channel: Channel) -> int:
    """Videos that can still open a Short on this channel."""
    return len(hero.candidates(conn, settings, channel.id, 10 ** 9, use_clip=False))


def restock(conn: sqlite3.Connection, settings: Settings, channel: Channel, need: int = 0) -> int:
    """Download, cut and embed new footage when fewer than settings.restock_below (or `need`)
    unused hero videos are left. Returns the number of new videos."""
    left = unused_hero_videos(conn, settings, channel)
    if left >= max(settings.restock_below, need):
        return 0
    from .embed import embed_pending
    from .ingest_nasa import ingest
    from .scenes import process_all

    log.info("only %d unused hero videos left: downloading more footage", left)
    new = ingest(conn, settings, channel.search_terms, per_term=settings.restock_per_term)
    if new == 0:
        log.warning("no new footage found for the search terms of '%s': add more search_terms to "
                    "config/channels/%s.toml (or free disk space)", channel.id, channel.id)
        return 0
    process_all(conn, settings)
    embed_pending(conn, settings)
    log.info("added %d videos; %d unused hero videos now", new, unused_hero_videos(conn, settings, channel))
    return new


def cleanup_candidates(conn: sqlite3.Connection) -> list[tuple[sqlite3.Row, str]]:
    """Downloaded videos whose file can go: duplicates, videos without a single usable scene, and
    videos whose usable scenes are all used by Shorts that are uploaded or rejected. A video that a
    Short still waiting for review, approval or upload uses is always kept (rerender needs it)."""
    out = []
    for a in conn.execute("SELECT * FROM assets WHERE path IS NOT NULL AND status != 'cleaned'").fetchall():
        if not Path(a["path"]).exists():
            continue
        if a["status"] not in ("scened", "duplicate"):
            continue  # not cut into scenes yet, or failed: leave it alone
        # Shorts (any channel) that use a scene of this video, and their state.
        states = [r[0] for r in conn.execute(
            """SELECT DISTINCT s.status FROM shorts s
                WHERE s.hero_scene_id IN (SELECT id FROM scenes WHERE asset_id=?)
                   OR s.id IN (SELECT b.short_id FROM blocks b JOIN scenes sc ON sc.id=b.scene_id
                                WHERE sc.asset_id=?)""", (a["id"], a["id"]))]
        if any(st not in DONE_STATES for st in states):
            continue
        if a["status"] == "duplicate":
            out.append((a, "duplicate"))
            continue
        usable = conn.execute("SELECT COUNT(*) FROM scenes WHERE asset_id=? AND rejected=0",
                              (a["id"],)).fetchone()[0]
        if usable == 0:
            out.append((a, "no usable scenes"))
            continue
        unused = conn.execute(
            """SELECT COUNT(*) FROM scenes sc WHERE sc.asset_id=? AND sc.rejected=0
                 AND sc.id NOT IN (SELECT hero_scene_id FROM shorts
                                    WHERE status!='rejected' AND hero_scene_id IS NOT NULL)
                 AND sc.id NOT IN (SELECT b.scene_id FROM blocks b JOIN shorts s ON s.id=b.short_id
                                    WHERE s.status!='rejected' AND b.scene_id IS NOT NULL)""",
            (a["id"],)).fetchone()[0]
        if unused == 0:
            out.append((a, "all scenes used"))
    return out


def cleanup(conn: sqlite3.Connection, dry_run: bool = False) -> tuple[int, float]:
    """Delete the files of cleanup_candidates(). The database row and fingerprint stay, so the same
    NASA video (or a re-published copy of it) is never downloaded again. Returns (files, GB)."""
    n, size = 0, 0.0
    for a, why in cleanup_candidates(conn):
        path = Path(a["path"])
        gb = path.stat().st_size / 1e9
        print(f"{'would delete' if dry_run else 'deleted'} {path.name} ({gb:.2f} GB, {why}): {a['title']}")
        n += 1
        size += gb
        if dry_run:
            continue
        path.unlink()
        conn.execute("UPDATE assets SET status='cleaned', path=NULL, error=? WHERE id=?",
                     (f"file deleted by cleanup ({why})", a["id"]))
        # Its scenes can't be rendered any more: take them out of hero picks and footage search.
        conn.execute("""UPDATE scenes SET rejected=1, reject_reason='file deleted'
                         WHERE asset_id=? AND rejected=0""", (a["id"],))
        conn.commit()
    return n, size
