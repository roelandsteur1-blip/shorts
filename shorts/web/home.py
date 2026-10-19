"""Home: one card per channel."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, redirect, render_template, request, url_for

from ..hero import shorts_left
from .common import channels, conn, settings

bp = Blueprint("home", __name__)


def channel_card(ch) -> dict:
    c = conn()
    counts = dict(c.execute("SELECT status, COUNT(*) FROM shorts WHERE channel=? GROUP BY status",
                            (ch.id,)).fetchall())
    now = datetime.now(timezone.utc)
    week = c.execute("""SELECT COUNT(*) FROM shorts WHERE channel=? AND status='scheduled'
                          AND publish_at >= ? AND publish_at < ?""",
                     (ch.id, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()[0]
    left, videos = shorts_left(c, settings(), ch.id, fits=ch)
    last_job = c.execute("SELECT * FROM jobs WHERE channel=? ORDER BY id DESC LIMIT 1", (ch.id,)).fetchone()
    return {
        "ch": ch,
        "connected": (settings().secrets_dir / f"token-{ch.id}.json").exists(),
        "review": counts.get("rendered", 0),
        "approved": counts.get("approved", 0),
        "failed": counts.get("failed", 0),
        "scheduled_week": week,
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
    c, s, out = conn(), settings(), []
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
        planned = {r[0] for r in c.execute("""SELECT substr(publish_at, 1, 10) FROM shorts WHERE channel=?
                                                AND status='scheduled' AND publish_at >= ?""",
                                           (ch.id, days[0].isoformat()))}
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
    cards = [channel_card(ch) for ch in channels()]
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


@bp.post("/speed")
def set_speed():
    """Quiet / Fast switch (nav bar). Running jobs pick it up before their next video or render."""
    from ..speed import set_mode
    mode = request.form.get("mode")
    if mode in ("quiet", "fast"):
        set_mode(settings().data_dir, mode)
    target = request.referrer or "/"
    return redirect(target if target.startswith(request.host_url) or target.startswith("/") else "/")
