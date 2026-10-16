"""Schedule: approved, scheduled and published Shorts per channel, the next free slots and the
shared daily upload quota."""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import groupby

from flask import Blueprint, abort, redirect, render_template, request, url_for

from ..upload import iso, next_slots, uploads_today
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
        slots = [iso(s) for s in next_slots(c, ch, len(approved))] if approved else []
        days = [(day, list(rows)) for day, rows in groupby(upcoming, key=lambda r: r["publish_at"][:10])]
        out.append({"ch": ch, "approved": list(zip(approved, slots)), "days": days, "published": published})
    used = uploads_today(c)
    return render_template("schedule.html", rows=out, used=used, quota=settings().upload_quota,
                           channels=channels(), selected=selected, daily=daily_status(),
                           msg=request.args.get("msg"))


def daily_status() -> dict:
    from .. import daily
    return daily.status(settings())


@bp.post("/schedule/daily")
def set_daily():
    from .. import daily
    try:
        msg = daily.set_task(settings(), request.form.get("on") == "1", (request.form.get("time") or "09:00").strip())
    except (ValueError, RuntimeError) as e:
        abort(400, str(e))
    return redirect(url_for("schedule.index", msg=msg))
