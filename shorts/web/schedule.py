"""Schedule: approved, scheduled and published Shorts per channel, the next free slots and the
shared daily upload quota."""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import groupby

from flask import Blueprint, render_template, request

from ..upload import blocked_until, channel_uploads_last_day, iso, slots_for, uploads_today
from .common import channels, conn, settings

bp = Blueprint("schedule", __name__)


@bp.get("/schedule")
def index():
    c = conn()
    selected = request.args.get("channel") or None
    now = iso(datetime.now(timezone.utc))
    out = []
    for ch in channels():
        if selected and ch.id != selected:
            continue
        approved = c.execute("SELECT * FROM shorts WHERE channel=? AND status='approved' ORDER BY id",
                             (ch.id,)).fetchall()
        upcoming = c.execute("""SELECT * FROM shorts WHERE channel=? AND status='scheduled' AND publish_at >= ?
                                 ORDER BY publish_at""", (ch.id, now)).fetchall()
        published = c.execute("""SELECT * FROM shorts WHERE channel=? AND youtube_id IS NOT NULL
                                   AND (publish_at < ? OR status='uploaded') ORDER BY COALESCE(publish_at, uploaded_at) DESC
                                  LIMIT 20""", (ch.id, now)).fetchall()
        slots = [iso(s) for s in slots_for(c, settings(), ch, [r["id"] for r in approved])]
        days = [(day, list(rows)) for day, rows in groupby(upcoming, key=lambda r: r["publish_at"][:10])]
        out.append({"ch": ch, "approved": list(zip(approved, slots)), "days": days, "published": published,
                    "today": channel_uploads_last_day(c, ch), "blocked": blocked_until(settings(), ch)})
    used = uploads_today(c)
    return render_template("schedule.html", rows=out, used=used, quota=settings().upload_quota,
                           per_channel=settings().channel_uploads_per_day,
                           channels=channels(), selected=selected,
                           msg=request.args.get("msg"))


@bp.post("/schedule/upload-all")
def upload_all():
    from .jobs import start_job
    return start_job(None, ["upload-all"], "Daily upload, all channels")

