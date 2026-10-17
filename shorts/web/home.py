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
    left, videos = shorts_left(c, settings(), ch.id, ch.categories)
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
        "categories": [(cat, shorts_left(c, settings(), ch.id, [cat])[0]) for cat in ch.categories],
        "last_job": last_job,
    }


@bp.get("/")
def index():
    running = conn().execute("SELECT * FROM jobs WHERE status IN ('running','queued') ORDER BY id").fetchall()
    return render_template("home.html", cards=[channel_card(ch) for ch in channels()], running=running,
                           per_channel=settings().channel_uploads_per_day, msg=request.args.get("msg"))


@bp.post("/make-all")
def make_all():
    """One click: top every channel up to `count` Shorts waiting (to review or approved, not uploaded
    yet). Channels that already have enough are skipped; the jobs run one after another."""
    from .. import jobs
    try:
        target = max(1, min(100, int(request.form.get("count") or 2)))
    except ValueError:
        target = 2
    started, enough = 0, []
    for ch in channels():
        have = conn().execute("SELECT COUNT(*) FROM shorts WHERE channel=? AND status IN ('rendered','approved')",
                              (ch.id,)).fetchone()[0]
        if have >= target:
            enough.append(f"{ch.name} ({have})")
            continue
        n = target - have
        jobs.submit(conn(), ch.id, ["make", "-n", str(n)], f"Make {n} Shorts (to {target})")
        started += 1
    if not started:
        return redirect(url_for("home.index", msg=f"Every channel already has {target} or more: " + ", ".join(enough)))
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
