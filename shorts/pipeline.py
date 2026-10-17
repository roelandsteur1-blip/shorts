"""Runs the 'make' steps. Every step is resumable: it picks up Shorts by status.

The steps overlap instead of waiting for each other: while Claude writes the scripts of the next
batch, the previous batch is fact-checked, and every checked Short goes straight to rendering
(several at once in Fast mode). Waiting on Claude and rendering on the CPU/GPU then happen at the
same time, so a batch takes about as long as its Claude calls. Several channels can share one run
(make_many): Claude writes channel B while channel A renders. Meanwhile, in the background: footage
downloads for the next restock, the music check, and loading CLIP/Kokoro for the first render."""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from . import db, factcheck, hero, llm, progress, render, script, shots, speed, voice
from .config import Channel, Settings

log = logging.getLogger(__name__)

_DONE = None  # end-of-stream marker on the queues
REFILL_ROUNDS = 3  # times Make picks new hero shots for ones Claude skipped as off-niche
# Shorts that count as "waiting" when Make for every channel tops channels up (failed ones are
# retried by every run, so they don't count).
WAITING = ("scripted", "checked", "rendered", "approved")


@dataclass
class Lane:
    """One channel's part of a run: `count` new Shorts (0 = only finish what an earlier run left)."""
    channel: Channel
    fmt_id: str | None
    count: int


def make(conn: sqlite3.Connection, settings: Settings, channel: Channel, count: int,
         format_id: str | None = None, restock: bool = True) -> list[int]:
    make_many(conn, settings, [(channel, count, format_id)], restock=restock)
    return [r[0] for r in conn.execute(
        "SELECT id FROM shorts WHERE channel=? AND status='rendered' ORDER BY id", (channel.id,))]


def top_up_orders(conn: sqlite3.Connection, channels: list[Channel], target: int
                  ) -> tuple[list[tuple[Channel, int, None]], list[str]]:
    """'Make for every channel': how many each channel needs to have `target` Shorts waiting (to
    review or approved, or still being made). Returns (orders, names of channels with enough)."""
    orders, enough = [], []
    marks = ",".join("?" * len(WAITING))
    for ch in channels:
        have = conn.execute(f"SELECT COUNT(*) FROM shorts WHERE channel=? AND status IN ({marks})",
                            (ch.id, *WAITING)).fetchone()[0]
        if have >= target:
            enough.append(f"{ch.name} ({have})")
        else:
            orders.append((ch, target - have, None))
    return orders, enough


def make_many(conn: sqlite3.Connection, settings: Settings, orders: list[tuple[Channel, int, str | None]],
              restock: bool = True) -> dict[str, int]:
    """Make Shorts for several channels in one pipeline. orders: (channel, count, format id or
    None). Returns Shorts waiting for review per channel."""
    if not orders:
        return {}
    from .sources.common import recheck_rights
    if flagged := recheck_rights(conn):
        log.info("%d videos newly flagged for a rights check; they won't be used", flagged)
    # Scenes cut since the last run (also by a run stopped before embedding) get their CLIP
    # embedding and categories.
    from .categorize import sort_pending
    from .embed import embed_pending
    embed_pending(conn, settings)
    sort_pending(conn, settings)

    from . import library
    prefetched = None
    if restock:
        # 1. Only when the library can't even give this batch: download first.
        for ch, count, _ in orders:
            library.restock(conn, settings, ch, need=count, only_if_short=True)
        # Downloads for the restock after this batch start now, while Claude writes.
        prefetched = library.prefetch(conn, settings, [(ch, count) for ch, count, _ in orders])

    channels = [ch for ch, _, _ in orders]
    # Music (tag tracks added by hand, fetch more when the library runs low) and loading the models
    # happen next to the first Claude calls; the first render waits for the music check.
    music_check = threading.Thread(target=_music_check, args=(settings, channels[0]), name="music", daemon=True)
    music_check.start()
    threading.Thread(target=_warm_up, args=(settings, channels), name="warm-up", daemon=True).start()

    # 2-5. Hero shots -> scripts -> fact check -> footage + render, overlapping (and channel after
    #      channel for the Claude steps).
    lanes = [Lane(ch, fmt or ch.enabled_formats[0]["id"], count) for ch, count, fmt in orders]
    run_steps(conn, settings, lanes, before_render=music_check)
    music_check.join()  # also when nothing rendered: don't cut a music download off at exit

    # 6. Refill the library for next time (below restock_below), after this batch is reviewable.
    if prefetched:
        library.finish_prefetch(conn, settings, prefetched)
    if restock:
        for ch in channels:
            library.restock(conn, settings, ch)
    return {ch.id: conn.execute("SELECT COUNT(*) FROM shorts WHERE channel=? AND status='rendered'",
                                (ch.id,)).fetchone()[0] for ch in channels}


def _music_check(settings: Settings, channel: Channel) -> None:
    from . import music
    progress.mute()
    c = db.connect(settings.db_path)
    try:
        music.restock(c, settings, channel)
    finally:
        c.close()


def _warm_up(settings: Settings, channels: list[Channel]) -> None:
    """Load what the first render needs while Claude writes: GPU checks, CLIP, Kokoro."""
    from . import embed, media
    try:
        if settings.encoder in ("auto", "h264_nvenc"):
            media._nvenc_works()
            media.nvdec_works()
        embed.get_encoder(settings)
        if voices := {v for v in (voice.channel_voice(ch) for ch in channels) if v}:
            voice.warm_up(voices)
    except Exception:  # noqa: BLE001 - only a head start: the real step loads it again if needed
        log.debug("warm-up failed", exc_info=True)


def finish(conn: sqlite3.Connection, settings: Settings, channel: Channel) -> None:
    """Fact check, footage per block and render for everything that's not done yet."""
    run_steps(conn, settings, [Lane(channel, None, 0)])


def _ids(conn: sqlite3.Connection, channel: Channel, *statuses: str) -> list[int]:
    return [r[0] for st in statuses for r in conn.execute(
        "SELECT id FROM shorts WHERE channel=? AND status=? ORDER BY id", (channel.id, st))]


def _write_lane(c: sqlite3.Connection, settings: Settings, lane: Lane, total: list[int], on_batch) -> None:
    """Pick the lane's hero shots and write their scripts. Picked here, right before writing, so
    the scripts of the channels before it are saved and one-channel-per-video still holds."""
    ch = lane.channel
    pending = hero.candidates(c, settings, ch.id, lane.count, categories=ch.categories, query=ch.hero_query)
    total[0] -= lane.count - len(pending)
    if not pending:
        log.warning("%s: no unused hero shots left: run 'shorts ingest' to grow the library", ch.id)
        return
    log.info("%s: picked %d hero shots", ch.id, len(pending))
    for _ in range(REFILL_ROUNDS + 1):
        made = script.write_scripts(c, settings, ch, lane.fmt_id, pending, on_batch=on_batch)
        missing = len(pending) - len(made)
        if not missing or llm.limit_reached():
            break
        # New hero shots for ones that gave no script (off-niche videos are now excluded).
        pending = hero.candidates(c, settings, ch.id, missing, categories=ch.categories, query=ch.hero_query)
        if not pending:
            break
        log.info("%s: picked %d more hero shots for the ones skipped", ch.id, len(pending))


def run_steps(conn: sqlite3.Connection, settings: Settings, lanes: list[Lane],
              before_render: threading.Thread | None = None) -> None:
    """Scripts for each lane, plus everything left halfway by an earlier run ('scripted' ->
    fact check, 'checked'/'failed' -> render). Three lanes of work: Claude writes (channel after
    channel), Claude checks, the main thread picks footage and hands renders to a pool. Each has
    its own database connection. before_render: a thread the first render waits for."""
    to_check: queue.Queue = queue.Queue()
    to_render: queue.Queue = queue.Queue()
    channels = {lane.channel.id: lane.channel for lane in lanes}
    leftover: list[list[int]] = []
    for lane in lanes:
        for sid in _ids(conn, lane.channel, "checked", "failed"):
            to_render.put(sid)
        ids = _ids(conn, lane.channel, "scripted")
        leftover += [ids[i:i + 5] for i in range(0, len(ids), 5)]
    writing = [lane for lane in lanes if lane.count > 0]
    total = [sum(lane.count for lane in writing) + sum(map(len, leftover)) + to_render.qsize()]
    if not total[0]:
        return

    def write() -> None:
        c = db.connect(settings.db_path)
        try:
            if writing:
                progress.report(0, total[0], "script")
            for lane in writing:
                if llm.limit_reached():
                    total[0] -= lane.count
                    continue
                try:
                    _write_lane(c, settings, lane, total, to_check.put)
                except Exception:  # noqa: BLE001 - heroes without a script are picked again next run
                    log.exception("%s: writing scripts failed", lane.channel.id)
        finally:
            to_check.put(_DONE)
            c.close()

    def check() -> None:
        c = db.connect(settings.db_path)
        try:
            while True:
                batch = leftover.pop(0) if leftover else to_check.get()
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
    _render_lane(conn, settings, channels, to_render, total, before_render)
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


def _render_lane(conn: sqlite3.Connection, settings: Settings, channels: dict[str, Channel],
                 to_render: queue.Queue, total: list[int], before_render: threading.Thread | None) -> None:
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
                    channel = channels[conn.execute("SELECT channel FROM shorts WHERE id=?", (sid,)).fetchone()[0]]
                    try:
                        # Voice first: its length decides how long each block (and its shot) is.
                        voice.prepare(conn, settings, channel, sid)
                        shots.assign(conn, settings, sid, channel.categories)
                    except Exception as e:  # noqa: BLE001
                        log.exception("short %d: no footage", sid)
                        db.set_short(conn, sid, status="failed", error=str(e)[:1000])
                        done += 1
                        continue
                    if before_render is not None:
                        before_render.join()  # music check done, so the render can pick a track
                        before_render = None
                    running.add(pool.submit(_render_one, settings, channel, sid))
                    continue
            if running:
                finished, _ = wait(running, timeout=0.5, return_when=FIRST_COMPLETED)
                for f in finished:
                    running.discard(f)
                    done += 1
                    progress.report(done, max(total[0], done), "render")
