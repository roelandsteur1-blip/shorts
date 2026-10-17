"""Runs the 'make' steps. Every step is resumable: it picks up Shorts by status.

The steps overlap instead of waiting for each other: while Claude writes the scripts of the next
batch, the previous batch is fact-checked, and every checked Short goes straight to rendering
(several at once in Fast mode). Waiting on Claude and rendering on the CPU/GPU then happen at the
same time, so a batch takes about as long as its Claude calls."""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import db, factcheck, hero, llm, progress, render, script, shots, speed
from .config import Channel, Settings

log = logging.getLogger(__name__)

_DONE = None  # end-of-stream marker on the queues


def make(conn: sqlite3.Connection, settings: Settings, channel: Channel, count: int,
         format_id: str | None = None, restock: bool = True) -> list[int]:
    fmt_id = format_id or channel.enabled_formats[0]["id"]
    from .sources.common import recheck_rights
    if flagged := recheck_rights(conn):
        log.info("%d videos newly flagged for a rights check; they won't be used", flagged)
    # Scenes cut since the last run get their categories (cheap: metadata + CLIP).
    from .categorize import sort_pending
    sort_pending(conn, settings)

    # 1. Only when the library can't even give this batch: download first. Topping up the stock
    #    happens after rendering (step 6), when the Shorts are already waiting for review.
    if restock:
        from .library import restock as restock_library
        restock_library(conn, settings, channel, need=count, only_if_short=True)

    # 2. Hero shots first; the script is written around them.
    heroes = hero.candidates(conn, settings, channel.id, count, categories=channel.categories,
                             query=channel.hero_query)
    if not heroes:
        log.warning("no unused hero shots left: run 'shorts ingest' to grow the library")
    else:
        log.info("picked %d hero shots", len(heroes))

    # Music: tag tracks added by hand, fetch more when the channel runs low (before rendering).
    from . import music
    music.restock(conn, settings, channel)

    # 3-5. Scripts -> fact check -> footage + render, overlapping.
    run_steps(conn, settings, channel, fmt_id, heroes)

    # 6. Refill the library for next time (below restock_below), after this batch is reviewable.
    if restock:
        from .library import restock as restock_library
        restock_library(conn, settings, channel)
    return [r[0] for r in conn.execute(
        "SELECT id FROM shorts WHERE channel=? AND status='rendered' ORDER BY id", (channel.id,))]


def finish(conn: sqlite3.Connection, settings: Settings, channel: Channel) -> None:
    """Fact check, footage per block and render for everything that's not done yet."""
    run_steps(conn, settings, channel, None, [])


def _ids(conn: sqlite3.Connection, channel: Channel, *statuses: str) -> list[int]:
    return [r[0] for st in statuses for r in conn.execute(
        "SELECT id FROM shorts WHERE channel=? AND status=? ORDER BY id", (channel.id, st))]


def run_steps(conn: sqlite3.Connection, settings: Settings, channel: Channel, fmt_id: str | None,
              heroes: list[int]) -> None:
    """Scripts for `heroes`, plus everything left halfway by an earlier run ('scripted' ->
    fact check, 'checked'/'failed' -> render). Three lanes: Claude writes, Claude checks, the main
    thread picks footage and hands renders to a pool. Each lane has its own database connection."""
    to_check: queue.Queue = queue.Queue()
    to_render: queue.Queue = queue.Queue()
    for sid in _ids(conn, channel, "checked", "failed"):
        to_render.put(sid)
    leftover = _ids(conn, channel, "scripted")
    expected = len(heroes) + len(leftover) + to_render.qsize()
    if not expected:
        return

    def write() -> None:
        c = db.connect(settings.db_path)
        try:
            if heroes:
                progress.report(0, expected, "script")
                script.write_scripts(c, settings, channel, fmt_id, heroes, on_batch=to_check.put)
        except Exception:  # noqa: BLE001 - heroes without a script are picked again next run
            log.exception("writing scripts failed")
        finally:
            to_check.put(_DONE)
            c.close()

    def check() -> None:
        c = db.connect(settings.db_path)
        try:
            batches = [leftover[i:i + 5] for i in range(0, len(leftover), 5)]
            while True:
                batch = batches.pop(0) if batches else to_check.get()
                if batch is _DONE:
                    break
                factcheck.check(c, settings, batch)
                for sid in batch:  # unanswered ones stay 'scripted' and are retried next run
                    if c.execute("SELECT status FROM shorts WHERE id=?", (sid,)).fetchone()[0] == "checked":
                        to_render.put(sid)
                if llm.limit_reached():
                    log.warning("Claude usage limit: the remaining scripts wait for the next run")
        except Exception:  # noqa: BLE001
            log.exception("fact check failed")
        finally:
            to_render.put(_DONE)
            c.close()

    writer = threading.Thread(target=write, name="claude-write", daemon=True)
    checker = threading.Thread(target=check, name="claude-check", daemon=True)
    writer.start()
    checker.start()
    _render_lane(conn, settings, channel, to_render, expected)
    writer.join()
    checker.join()


def _render_one(settings: Settings, channel: Channel, sid: int) -> None:
    c = db.connect(settings.db_path)
    try:
        out = render.render_short(c, settings, channel, sid)
        log.info("rendered short %d -> %s", sid, out)
    except Exception as e:  # noqa: BLE001 - mark and keep going with the rest of the batch
        log.exception("short %d failed", sid)
        db.set_short(c, sid, status="failed", error=str(e)[:1000])
    finally:
        c.close()


def _render_lane(conn: sqlite3.Connection, settings: Settings, channel: Channel,
                 to_render: queue.Queue, expected: int) -> None:
    """Footage per block (main thread: the CLIP search isn't shared between threads), then the
    render itself in a pool. How many render at once follows the Quiet/Fast switch."""
    from .media import _nvenc_works
    nvenc = settings.encoder in ("auto", "h264_nvenc") and _nvenc_works()
    gate = speed.Gate(settings.data_dir, "render_workers", nvenc=nvenc)
    done, open_lane = 0, True
    running: set = set()
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="render") as pool:
        while open_lane or running:
            if open_lane and len(running) < gate.allowed():
                try:
                    sid = to_render.get(timeout=0.5)
                except queue.Empty:
                    sid = -1
                if sid is _DONE:
                    open_lane = False
                elif sid != -1:
                    try:
                        shots.assign(conn, settings, sid, channel.categories)
                    except Exception as e:  # noqa: BLE001
                        log.exception("short %d: no footage", sid)
                        db.set_short(conn, sid, status="failed", error=str(e)[:1000])
                        done += 1
                        continue
                    running.add(pool.submit(_render_one, settings, channel, sid))
                    continue
            if running:
                finished, _ = wait(running, timeout=0.5, return_when=FIRST_COMPLETED)
                for f in finished:
                    running.discard(f)
                    done += 1
                    progress.report(done, expected, "render")
