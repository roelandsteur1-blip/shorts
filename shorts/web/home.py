"""Home: one card per channel."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import Blueprint, redirect, render_template, request, url_for

from . import cache
from ..upload import token_path
from .common import channel_topic, channels, conn, settings, shown_channels

bp = Blueprint("home", __name__)
log = logging.getLogger(__name__)

YT_CACHE_SECONDS = 600  # Home asks YouTube for the schedule at most this often per channel
_yt_cache: dict[str, tuple[float, str | None, list[dict]]] = {}
_refreshing: set[str] = set()   # channels whose schedule is being fetched in the background
_lock = threading.Lock()


def _last_upload(c, ch) -> str | None:
    return c.execute("SELECT MAX(uploaded_at) FROM shorts WHERE channel=?", (ch.id,)).fetchone()[0]


def _known_ids(c, ch) -> list[str]:
    # Every video id we know for the channel, not only ones we think are scheduled: YouTube's
    # uploads list can lag behind fresh uploads, and a status here can be out of date.
    return [r[0] for r in c.execute(
        """SELECT youtube_id FROM shorts WHERE channel=? AND youtube_id IS NOT NULL
           UNION SELECT youtube_id FROM yt_videos WHERE channel=?""", (ch.id, ch.id))]


def _fetch(s, ch, known):
    from .. import ytstats
    try:
        return ytstats.scheduled(s, ch, known)
    except Exception as e:  # noqa: BLE001 - offline, expired login, quota: show our own data
        log.warning("%s: could not read the schedule from YouTube: %s", ch.id, e)
        return None


def _store(c, ch, last, res) -> None:
    _yt_cache[ch.id] = (time.monotonic(), last, res[0])
    # the same answer carries the subscriber count: keep it for the card and Stats
    c.execute("""INSERT INTO yt_sync (channel, subscribers, subscribers_at) VALUES (?, ?, datetime('now'))
                 ON CONFLICT(channel) DO UPDATE SET subscribers=excluded.subscribers,
                     subscribers_at=excluded.subscribers_at""", (ch.id, res[1]))


def refresh_in_background(s, chs) -> None:
    """Fetch these channels' schedules from YouTube in a background thread (its own database
    connection), so no page waits for YouTube. A channel already being fetched is skipped."""
    from .. import db

    with _lock:
        chs = [ch for ch in chs if ch.id not in _refreshing]
        _refreshing.update(ch.id for ch in chs)
    if not chs:
        return

    def run():
        try:
            c = db.connect(s.db_path)
            try:
                jobs = [(ch, _known_ids(c, ch), _last_upload(c, ch)) for ch in chs]
                with ThreadPoolExecutor(max_workers=min(len(jobs), 5)) as pool:
                    results = list(pool.map(lambda j: _fetch(s, j[0], j[1]), jobs))
                for (ch, _, last), res in zip(jobs, results):
                    if res is not None:
                        _store(c, ch, last, res)
                c.commit()
            finally:
                c.close()
        except Exception:  # noqa: BLE001 - a background refresh must never take the dashboard down
            log.exception("background schedule refresh failed")
        finally:
            with _lock:
                _refreshing.difference_update(ch.id for ch in chs)

    threading.Thread(target=run, name="yt-schedule", daemon=True).start()


def warm_up(s, chs) -> None:
    """At start: fetch every connected channel's schedule, so the first Home visit is fast too."""
    refresh_in_background(s, [ch for ch in chs if token_path(s, ch.id).exists()])


def youtube_schedule(chs) -> dict[str, list[dict] | None]:
    """Scheduled videos per connected channel, straight from YouTube. An answer up to
    YT_CACHE_SECONDS old is used as it is; an older one is still shown while a background thread
    asks YouTube again, so pages never wait on YouTube once it answered. Only a channel never asked
    yet, or one with a new upload since (by a job in another process), is fetched while the page
    waits (channels side by side). None = YouTube couldn't be asked: Home falls back to our data."""
    s, c, out, todo, stale = settings(), conn(), {}, [], []
    for ch in chs:
        if not token_path(s, ch.id).exists():
            continue
        last = _last_upload(c, ch)
        hit = _yt_cache.get(ch.id)
        if hit and hit[1] == last:
            out[ch.id] = hit[2]
            if time.monotonic() - hit[0] >= YT_CACHE_SECONDS:
                stale.append(ch)
        else:
            todo.append((ch, _known_ids(c, ch), last))

    if todo:
        with ThreadPoolExecutor(max_workers=min(len(todo), 5)) as pool:
            for (ch, _, last), res in zip(todo, pool.map(lambda j: _fetch(s, j[0], j[1]), todo)):
                out[ch.id] = res and res[0]
                if res is not None:
                    _store(c, ch, last, res)
        c.commit()
    if stale:
        refresh_in_background(s, stale)
    return out


def _our_schedule(ch, since: str) -> list[str]:
    return [r[0] for r in conn().execute("""SELECT publish_at FROM shorts WHERE channel=? AND status='scheduled'
                                              AND publish_at >= ?""", (ch.id, since))]


def channel_card(ch, yt: list[dict] | None = None) -> dict:
    c = conn()
    counts = dict(c.execute("SELECT status, COUNT(*) FROM shorts WHERE channel=? GROUP BY status",
                            (ch.id,)).fetchall())
    now = datetime.now(timezone.utc)
    start, end = now.strftime("%Y-%m-%dT%H:%M:%SZ"), (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    planned = [v["publish_at"] for v in yt] if yt is not None else _our_schedule(ch, start)
    week = sum(start <= p < end for p in planned)
    foot = cache.footage(ch)
    last_job = c.execute("SELECT * FROM jobs WHERE channel=? ORDER BY id DESC LIMIT 1", (ch.id,)).fetchone()
    subs = c.execute("SELECT subscribers FROM yt_sync WHERE channel=?", (ch.id,)).fetchone()
    from ..metatype import NICHE_LABELS
    return {
        "ch": ch,
        "topic": NICHE_LABELS.get(channel_topic(ch), "Other"),
        "connected": token_path(settings(), ch.id).exists(),
        "review": counts.get("rendered", 0),
        "approved": counts.get("approved", 0),
        "failed": counts.get("failed", 0),
        "scheduled_week": week,
        "planned": planned,
        "week_times": ", ".join(datetime.fromisoformat(p.replace("Z", "+00:00")).strftime("%a %H:%M")
                                for p in sorted(planned) if start <= p < end),
        "from_youtube": yt is not None,
        "footage_left": foot["left"],
        "footage_videos": foot["videos"],
        "categories": [(cat, foot["categories"].get(cat, 0)) for cat in ch.categories],
        "last_job": last_job,
        "subscribers": subs[0] if subs else None,
    }


def warnings(cards: list[dict], now: datetime | None = None) -> list[dict]:
    """Things to fix: an expired YouTube login, a channel blocked by YouTube's upload limit, footage
    running low, and days in the coming week with nothing scheduled. Each: {channel, text, link}."""
    from ..upload import blocked_until, login_expired_path

    now = now or datetime.now(timezone.utc)
    s, out = settings(), []
    for card in cards:
        ch = card["ch"]
        if login_expired_path(s, ch.id).exists():
            out.append({"channel": ch, "text": "YouTube login expired: connect YouTube again",
                        "link": f"/channels/{ch.id}"})
        until = blocked_until(s, ch, now)
        if until:
            out.append({"channel": ch, "text": f"YouTube's daily upload limit reached; uploads resume after "
                                               f"{until.strftime('%a %H:%M')} UTC", "link": f"/schedule?channel={ch.id}"})
        if card["footage_left"] < s.restock_below:
            out.append({"channel": ch, "text": f"footage running low: {card['footage_left']} Shorts left "
                                               f"(from {card['footage_videos']} videos); the next Make downloads more",
                        "link": f"/make?channel={ch.id}"})
        if (ch.upload_via or "api") == "phone":
            from .. import drive
            if not drive.connected(s):
                out.append({"channel": ch, "text": "uploads via the phone, but Google Drive is not connected",
                            "link": f"/channels/{ch.id}"})
            from ..upload import can_manage
            if card["connected"] and not can_manage(s, ch):
                out.append({"channel": ch, "text": "uploads via the phone: press Connect YouTube again (Connect again) so "
                                                   "the server may set title, description, tags and publish time",
                            "link": f"/channels/{ch.id}"})
            late = conn().execute("""SELECT COUNT(*) FROM shorts WHERE channel=? AND status='on_drive'
                                     AND publish_at < ?""", (ch.id, now.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()[0]
            if late:
                out.append({"channel": ch, "text": f"{late} Short(s) on Drive are past their publish time and not "
                                                   "found on YouTube yet (post them with the words the file name starts with as title)",
                            "link": f"/channels/{ch.id}"})
        if not card["connected"]:
            continue  # not uploading yet: an empty schedule is expected
        days = [(now + timedelta(days=i)).date() for i in range(1, 8)]
        planned = {p[:10] for p in card["planned"]}
        empty = [d for d in days if d.isoformat() not in planned]
        if empty:
            when = ("the next 7 days" if len(empty) == len(days)
                    else ", ".join(d.strftime("%a %d %b") for d in empty))
            extra = f" ({card['approved']} approved waiting: press Daily upload)" if card["approved"] else ""
            out.append({"channel": ch, "text": f"nothing scheduled on {when}{extra}" if len(empty) < len(days)
                        else f"nothing scheduled in {when}{extra}", "link": f"/schedule?channel={ch.id}"})
    return out


@bp.get("/")
def index():
    running = conn().execute("SELECT * FROM jobs WHERE status IN ('running','queued') ORDER BY id").fetchall()
    chs = channels()
    yt = youtube_schedule(chs)
    cards = [channel_card(ch, yt.get(ch.id)) for ch in chs]
    shown = {ch.id for ch in shown_channels()}  # filter chips; Needs attention keeps every channel
    from ..drive import on_phone
    phone = on_phone(conn())
    phone_name = next((c.name for c in chs if phone and c.id == phone["channel"]), phone["channel"] if phone else None)
    return render_template("home.html", cards=[c for c in cards if c["ch"].id in shown], running=running, warnings=warnings(cards),
                           phone=phone, phone_name=phone_name,
                           per_channel=settings().channel_uploads_per_day, msg=request.args.get("msg"))


@bp.post("/make-all")
def make_all():
    """One click: top every channel up to `count` Shorts waiting (to review or approved, not uploaded
    yet). Channels that already have enough are skipped. One job for all of them: Claude writes the
    next channel while the previous one renders."""
    from .. import jobs
    from ..pipeline import top_up_orders
    try:
        target = max(1, min(100, int(request.form.get("count") or 2)))
    except ValueError:
        target = 2
    orders, enough = top_up_orders(conn(), channels(), target)
    if not orders:
        return redirect(url_for("home.index", msg=f"Every channel already has {target} or more: " + ", ".join(enough)))
    n = sum(count for _, count, _ in orders)
    jobs.submit(conn(), None, ["make-all", "--to", str(target)],
                f"Make {n} Shorts for {len(orders)} channels (to {target} each)" if n else
                f"Finish Shorts an earlier run left unrendered ({len(orders)} channels)")
    return redirect(url_for("jobs.job_list"))

