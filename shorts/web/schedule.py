"""Schedule: approved, scheduled and published Shorts per channel, the next free slots and the
shared daily upload quota."""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import groupby

from flask import Blueprint, render_template, request

from ..upload import blocked_until, channel_uploads_last_day, iso, slots_for, uploads_today
from .common import channels, conn, settings
from .home import youtube_schedule

bp = Blueprint("schedule", __name__)


@bp.get("/schedule")
def index():
    c = conn()
    selected = request.args.get("channel") or None
    now = iso(datetime.now(timezone.utc))
    out = []
    chs = [ch for ch in channels() if not selected or ch.id == selected]
    yt = youtube_schedule(chs)  # the same answer Home counts from (cached a few minutes)
    for ch in chs:
        approved = c.execute("SELECT * FROM shorts WHERE channel=? AND status='approved' ORDER BY id",
                             (ch.id,)).fetchall()
        from_youtube = yt.get(ch.id) is not None
        upcoming = (_from_youtube(c, yt[ch.id]) if from_youtube else
                    [dict(r) for r in c.execute("""SELECT * FROM shorts WHERE channel=? AND status='scheduled'
                                                     AND publish_at >= ? ORDER BY publish_at""", (ch.id, now))])
        published = c.execute("""SELECT * FROM shorts WHERE channel=? AND youtube_id IS NOT NULL
                                   AND (publish_at < ? OR status='uploaded') ORDER BY COALESCE(publish_at, uploaded_at) DESC
                                  LIMIT 20""", (ch.id, now)).fetchall()
        slots = [iso(s) for s in slots_for(c, settings(), ch, [r["id"] for r in approved])]
        days = [(day, list(rows)) for day, rows in groupby(upcoming, key=lambda r: r["publish_at"][:10])]
        out.append({"ch": ch, "approved": list(zip(approved, slots)), "days": days, "from_youtube": from_youtube,
                    "published": published,
                    "today": channel_uploads_last_day(c, ch), "blocked": blocked_until(settings(), ch)})
    used = uploads_today(c)
    return render_template("schedule.html", rows=out, used=used, quota=settings().upload_quota,
                           per_channel=settings().channel_uploads_per_day,
                           channels=channels(), selected=selected,
                           msg=request.args.get("msg"))


def _from_youtube(c, videos: list[dict]) -> list[dict]:
    """What YouTube has scheduled, with our own Short where we have one. Our record follows
    YouTube: a publish time changed in Studio, or a status left at 'uploaded', is put right."""
    out = []
    for v in videos:
        own = c.execute("SELECT * FROM shorts WHERE youtube_id=?", (v["youtube_id"],)).fetchone()
        if own and (own["status"] in ("uploaded", "scheduled") and
                    (own["status"] != "scheduled" or own["publish_at"] != v["publish_at"])):
            c.execute("UPDATE shorts SET status='scheduled', publish_at=? WHERE id=?", (v["publish_at"], own["id"]))
        out.append({"id": own["id"] if own else None, "title": v["title"] or (own["title"] if own else ""),
                    "youtube_id": v["youtube_id"], "publish_at": v["publish_at"]})
    c.commit()
    return out


@bp.post("/schedule/upload-all")
def upload_all():
    from .jobs import start_job
    return start_job(None, ["upload-all"], "Daily upload, all channels")

