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


def shorts_left(conn: sqlite3.Connection, settings: Settings, channel: Channel,
                category: str | None = None) -> int:
    """Shorts the library can still open on this channel (in one of its categories, or in
    `category` only). A long video counts for several, see hero.budget()."""
    cats = [category] if category else channel.categories
    return hero.shorts_left(conn, settings, channel.id, cats)[0]


def restock_plan(conn: sqlite3.Connection, settings: Settings, channel: Channel,
                 need: int = 0) -> list[tuple[str, list[str]]]:
    """(source, search queries) to run: for each of the channel's categories that is running low,
    the category's search hints per source the channel uses. The channel's own search_terms are
    added for its first category (they describe its niche)."""
    from . import sources
    from .categorize import load_categories

    cats = {c.id: c for c in load_categories(settings)}
    wanted = [c for c in channel.categories if c in cats] or [None]
    total = shorts_left(conn, settings, channel)
    low = [c for c in wanted if total < need
           or shorts_left(conn, settings, channel, c) < settings.restock_below]
    plan: dict[str, list[str]] = {}
    for src in channel.sources or ["nasa"]:
        ok, why = sources.usable(settings, src)
        if not ok:
            log.info("restock skips %s: %s", src, why)
            continue
        for c in low:
            queries = cats[c].queries(src) if c else []
            if c == wanted[0]:
                queries = queries + channel.search_terms
            plan[src] = list(dict.fromkeys(plan.get(src, []) + queries))
    return [(src, qs) for src, qs in plan.items() if qs]


def restock(conn: sqlite3.Connection, settings: Settings, channel: Channel, need: int = 0,
            only_if_short: bool = False) -> int:
    """Download, cut, embed and sort new footage when the channel's categories have fewer than
    settings.restock_below (or, in total, `need`) Shorts left in the library. One download serves
    every channel whose categories it lands in. only_if_short: only when fewer than `need` are left
    (make uses this before picking, and tops up after rendering). Returns the number of new videos."""
    left = shorts_left(conn, settings, channel)
    if only_if_short and left >= need:
        return 0
    if left >= max(settings.restock_below, need) and all(
            shorts_left(conn, settings, channel, c) >= settings.restock_below
            for c in channel.categories):
        return 0
    from .categorize import sort_pending
    from .embed import embed_pending
    from .sources import common
    from .scenes import process_all

    plan = restock_plan(conn, settings, channel, need)
    log.info("footage for only %d more Shorts left: downloading more from %s", left,
             ", ".join(src for src, _ in plan) or "nowhere (no usable source)")
    # Downloads run in a thread with their own connection; scene cutting starts on the first
    # finished file instead of after the last one.
    import threading
    from . import db
    counts: list[int] = []

    def download() -> None:
        c = db.connect(settings.db_path)
        try:
            for src, queries in plan:
                counts.append(common.ingest(c, settings, src, queries, per_term=settings.restock_per_term))
        except Exception:  # noqa: BLE001
            log.exception("restock download failed")
        finally:
            c.close()

    t = threading.Thread(target=download, name="restock-download", daemon=True)
    t.start()
    process_all(conn, settings, more_coming=t.is_alive)
    t.join()
    new = sum(counts)
    if new == 0:
        log.warning("no new footage found for '%s': add search hints to its categories, more "
                    "search_terms or sources to config/channels/%s.toml (or free disk space)",
                    channel.id, channel.id)
        return 0
    process_all(conn, settings)  # anything that finished after the cutter's last look
    embed_pending(conn, settings)
    sort_pending(conn, settings)
    log.info("added %d videos; footage for %d Shorts now", new, shorts_left(conn, settings, channel))
    return new


def cleanup_candidates(conn: sqlite3.Connection, settings: Settings) -> list[tuple[sqlite3.Row, str]]:
    """Downloaded videos whose file can go: duplicates, videos without a single usable scene,
    videos whose usable scenes are all used, and videos that opened as many Shorts as they can
    (hero.budget(), or no hero-length scene left far enough from the earlier heroes). A video that a
    Short still waiting for review, approval or upload uses is always kept (rerender needs it)."""
    out = []
    heroes = hero.earlier_heroes(conn)  # all channels: the file is shared
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
            continue
        starts = [h["start"] for h in heroes.get(a["id"], [])]
        if not starts:
            continue  # never opened a Short: keep it for a hero (or as B-roll)
        if len(starts) >= hero.budget(settings, a["duration"]):
            out.append((a, f"{len(starts)} Shorts made, the most for its length"))
            continue
        spare = conn.execute("SELECT start FROM scenes WHERE asset_id=? AND rejected=0 AND end - start >= ?",
                             (a["id"], hero.MIN_HERO_SECONDS)).fetchall()
        if not any(hero.far_enough(settings, r["start"], starts) for r in spare):
            out.append((a, "no hero shot left far enough from the earlier ones"))
    return out


def cleanup(conn: sqlite3.Connection, settings: Settings, dry_run: bool = False) -> tuple[int, float]:
    """Delete the files of cleanup_candidates(). The database row and fingerprint stay, so the same
    NASA video (or a re-published copy of it) is never downloaded again. Returns (files, GB)."""
    n, size = 0, 0.0
    for a, why in cleanup_candidates(conn, settings):
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
