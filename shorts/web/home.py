"""Home: one card per channel."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import Blueprint, redirect, render_template, request, url_for

from ..hero import shorts_left
from ..upload import token_path
from .common import channels, conn, settings

bp = Blueprint("home", __name__)
log = logging.getLogger(__name__)

YT_CACHE_SECONDS = 600  # Home asks YouTube for the schedule at most this often per channel
_yt_cache: dict[str, tuple[float, list[dict]]] = {}


def youtube_schedule(chs) -> dict[str, list[dict] | None]:
    """Scheduled videos per connected channel, straight from YouTube (cached a few minutes, the
    channels fetched side by side). None = YouTube couldn't be asked: Home falls back to our data."""
    from .. import ytstats

    s, c, out, todo = settings(), conn(), {}, []
    for ch in chs:
        if not token_path(s, ch.id).exists():
            continue
        hit = _yt_cache.get(ch.id)
        if hit and time.monotonic() - hit[0] < YT_CACHE_SECONDS:
            out[ch.id] = hit[1]
        else:
            known = [r[0] for r in c.execute("SELECT youtube_id FROM shorts WHERE channel=? AND status='scheduled'"
                                             " AND youtube_id IS NOT NULL", (ch.id,))]
            todo.append((ch, known))

    def fetch(job):
        ch, known = job
        try:
            return ytstats.scheduled(s, ch, known)
        except Exception as e:  # noqa: BLE001 - offline, expired login, quota: show our own data
            log.warning("%s: could not read the schedule from YouTube: %s", ch.id, e)
            return None

    if todo:
        with ThreadPoolExecutor(max_workers=min(len(todo), 5)) as pool:
            for (ch, _), res in zip(todo, pool.map(fetch, todo)):
                out[ch.id] = res
                if res is not None:
                    _yt_cache[ch.id] = (time.monotonic(), res)
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
    left, videos = shorts_left(c, settings(), ch.id, fits=ch)
    last_job = c.execute("SELECT * FROM jobs WHERE channel=? ORDER BY id DESC LIMIT 1", (ch.id,)).fetchone()
    return {
        "ch": ch,
        "connected": token_path(settings(), ch.id).exists(),
        "review": counts.get("rendered", 0),
        "approved": counts.get("approved", 0),
        "failed": counts.get("failed", 0),
        "scheduled_week": week,
        "planned": planned,
        "from_youtube": yt is not None,
        "footage_left": left,
        "footage_videos": videos,
        "categories": [(cat, shorts_left(c, settings(), ch.id, [cat], fits=ch)[0]) for cat in ch.categories],
        "last_job": last_job,
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
    return render_template("home.html", cards=cards, running=running, warnings=warnings(cards),
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
                f"Make {n} Shorts for {len(orders)} channels (to {target} each)")
    return redirect(url_for("jobs.job_list"))

