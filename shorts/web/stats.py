"""Stats: views and retention per Short and per channel, from the 'stats' table (python -m shorts stats)."""
from __future__ import annotations

from flask import Blueprint, render_template, request

from .common import channels, conn

bp = Blueprint("stats", __name__)

LATEST = """SELECT s.id, s.channel, s.title, s.youtube_id, s.publish_at, s.format_id, st.views, st.engaged_views,
                   st.avg_view_pct, st.avg_view_dur, st.likes, st.shares, st.subs_gained, st.fetched_at
              FROM shorts s JOIN stats st ON st.short_id = s.id
             WHERE st.rowid = (SELECT MAX(rowid) FROM stats WHERE short_id = s.id)"""

SORTS = {"views": "views DESC", "retention": "avg_view_pct DESC", "engaged": "engaged_views * 1.0 / NULLIF(views, 0) DESC",
         "newest": "publish_at DESC"}


@bp.get("/stats")
def index():
    c = conn()
    sort = request.args.get("sort", "views")
    selected = request.args.get("channel") or None
    rows = c.execute(f"SELECT * FROM ({LATEST}) WHERE (? IS NULL OR channel = ?) ORDER BY {SORTS.get(sort, SORTS['views'])}",
                     (selected, selected)).fetchall()
    totals = c.execute(f"""SELECT channel, COUNT(*) AS shorts, SUM(views) AS views, SUM(subs_gained) AS subs,
                                  AVG(avg_view_pct) AS pct, SUM(engaged_views) * 1.0 / NULLIF(SUM(views), 0) AS engaged
                             FROM ({LATEST}) GROUP BY channel""").fetchall()
    by_format = c.execute(f"""SELECT channel, format_id, COUNT(*) AS shorts, AVG(views) AS views, AVG(avg_view_pct) AS pct
                                FROM ({LATEST}) GROUP BY channel, format_id ORDER BY channel, views DESC""").fetchall()
    from ..timing import claude_usage, summary
    return render_template("stats.html", rows=rows, totals=totals, by_format=by_format, sort=sort,
                           channels=channels(), selected=selected, timings=summary(c),
                           claude=claude_usage(c))
