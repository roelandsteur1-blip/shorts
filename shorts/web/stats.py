"""Stats: views and retention per Short and per channel, from the 'stats' table (python -m shorts stats)."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import median

from flask import Blueprint, render_template, request

from ..upload import AGES, LAG_DAYS
from .common import channels, conn

bp = Blueprint("stats", __name__)

LATEST = """SELECT s.id, s.channel, s.title, s.voice, s.youtube_id, s.publish_at, s.format_id, st.views, st.engaged_views,
                   st.avg_view_pct, st.avg_view_dur, st.likes, st.shares, st.subs_gained, st.fetched_at
              FROM shorts s JOIN stats st ON st.short_id = s.id
             WHERE st.rowid = (SELECT MAX(rowid) FROM stats WHERE short_id = s.id)"""

SORTS = {"views": "views DESC", "retention": "avg_view_pct DESC", "engaged": "engaged_views * 1.0 / NULLIF(views, 0) DESC",
         "newest": "publish_at DESC"}


COMPARE_SORTS = {"views": "Median views", "engaged": "Engaged", "viewed": "Avg viewed", "subs": "Subs per 1,000 views"}


def at_age(c: sqlite3.Connection, days: int) -> list[dict]:
    """Every Short with numbers for its first `days` days; 'reached' is the day that period ended."""
    rows = c.execute("""SELECT s.id, s.channel, s.voice, s.publish_at, s.created_at, a.views, a.engaged_views,
                               a.avg_view_pct, a.subs_gained
                          FROM stats_age a JOIN shorts s ON s.id = a.short_id WHERE a.days = ?""", (days,)).fetchall()
    return [dict(r) | {"reached": date.fromisoformat((r["publish_at"] or r["created_at"])[:10]) + timedelta(days=days - 1)}
            for r in rows]


def summarise(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    views = sum(r["views"] or 0 for r in rows)
    return {"shorts": len(rows), "views": median(r["views"] or 0 for r in rows),
            "engaged": sum(r["engaged_views"] or 0 for r in rows) / views if views else 0.0,
            "viewed": sum(r["avg_view_pct"] or 0 for r in rows) / len(rows),
            "subs": 1000 * sum(r["subs_gained"] or 0 for r in rows) / views if views else 0.0}


def compare_channels(c: sqlite3.Connection, days: int, sort: str = "views", today: date | None = None) -> list[dict]:
    """A row per channel, every Short taken at the same age, with the change from last week: Shorts
    that reached that age in the last 7 days against the 7 days before (Analytics lags LAG_DAYS)."""
    ref = (today or datetime.now(timezone.utc).date()) - timedelta(days=LAG_DAYS)
    by_channel: dict[str, list[dict]] = {}
    for r in at_age(c, days):
        by_channel.setdefault(r["channel"], []).append(r)
    out = []
    for ch, rows in by_channel.items():
        this = summarise([r for r in rows if ref - timedelta(days=7) < r["reached"] <= ref])
        last = summarise([r for r in rows if ref - timedelta(days=14) < r["reached"] <= ref - timedelta(days=7)])
        change = {k: this[k] - last[k] for k in COMPARE_SORTS} if this and last else {}
        out.append({"channel": ch, **summarise(rows), "change": change})
    key = sort if sort in COMPARE_SORTS else "views"
    return sorted(out, key=lambda r: -r[key])


@bp.get("/stats")
def index():
    c = conn()
    try:
        days = int(request.args.get("days", 3))
    except ValueError:
        days = 3
    days = days if days in AGES else 3
    csort = request.args.get("csort", "views")
    sort = request.args.get("sort", "views")
    selected = request.args.get("channel") or None
    rows = c.execute(f"SELECT * FROM ({LATEST}) WHERE (? IS NULL OR channel = ?) ORDER BY {SORTS.get(sort, SORTS['views'])}",
                     (selected, selected)).fetchall()
    totals = c.execute(f"""SELECT channel, COUNT(*) AS shorts, SUM(views) AS views, SUM(subs_gained) AS subs,
                                  AVG(avg_view_pct) AS pct, SUM(engaged_views) * 1.0 / NULLIF(SUM(views), 0) AS engaged
                             FROM ({LATEST}) GROUP BY channel""").fetchall()
    by_format = c.execute(f"""SELECT channel, format_id, COUNT(*) AS shorts, AVG(views) AS views, AVG(avg_view_pct) AS pct
                                FROM ({LATEST}) GROUP BY channel, format_id ORDER BY channel, views DESC""").fetchall()
    by_voice = c.execute(f"""SELECT voice IS NOT NULL AS voiced, COUNT(*) AS shorts, AVG(views) AS views,
                                    AVG(avg_view_pct) AS pct, SUM(engaged_views) * 1.0 / NULLIF(SUM(views), 0) AS engaged,
                                    AVG(subs_gained) AS subs
                               FROM ({LATEST}) GROUP BY voiced ORDER BY voiced DESC""").fetchall()
    from ..timing import claude_usage, summary
    return render_template("stats.html", compare=compare_channels(c, days, csort), days=days, ages=AGES,
                           csort=csort, compare_sorts=COMPARE_SORTS, rows=rows, totals=totals, by_format=by_format, by_voice=by_voice, sort=sort,
                           channels=channels(), selected=selected, timings=summary(c),
                           claude=claude_usage(c))


@bp.post("/stats/fetch-all")
def fetch_all():
    from .jobs import start_job
    return start_job(None, ["stats", "--all"], "Fetch stats, all channels")
