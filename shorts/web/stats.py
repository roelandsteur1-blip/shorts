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
             WHERE st.rowid = (SELECT MAX(rowid) FROM stats WHERE short_id = s.id) AND NOT s.stats_hidden"""

SORTS = {"views": "views DESC", "retention": "avg_view_pct DESC", "engaged": "engaged_views * 1.0 / NULLIF(views, 0) DESC",
         "newest": "publish_at DESC"}


COMPARE_SORTS = {"views": "Median views", "engaged": "Engaged", "viewed": "Avg viewed", "subs": "Subs per 1,000 views"}


def at_age(c: sqlite3.Connection, days: int) -> list[dict]:
    """Every Short with numbers for its first `days` days; 'reached' is the day that period ended."""
    rows = c.execute("""SELECT s.id, s.channel, s.voice, s.publish_at, s.created_at, a.views, a.engaged_views,
                               a.avg_view_pct, a.subs_gained
                          FROM stats_age a JOIN shorts s ON s.id = a.short_id
                         WHERE a.days = ? AND NOT s.stats_hidden""", (days,)).fetchall()
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


def voice_test(c: sqlite3.Connection, days: int) -> dict:
    """Voice-over vs text + music, fairly: each Short is scored against its own channel's median at
    the same age (1.0 = typical for that channel), so the channel's topic drops out. Only channels
    with both kinds of Shorts count; a channel that is all one kind says nothing about the voice."""
    by_channel: dict[str, list[dict]] = {}
    for r in at_age(c, days):
        by_channel.setdefault(r["channel"], []).append(r)
    views: dict[bool, list[float]] = {True: [], False: []}
    viewed: dict[bool, list[float]] = {True: [], False: []}
    channels_both, one_kind = [], 0
    for ch, rows in sorted(by_channel.items()):
        voiced, plain = [r for r in rows if r["voice"]], [r for r in rows if not r["voice"]]
        if not voiced or not plain:
            one_kind += 1
            continue
        mv, mp = median(r["views"] or 0 for r in rows), median(r["avg_view_pct"] or 0 for r in rows)
        for r in rows:
            views[bool(r["voice"])].append((r["views"] or 0) / mv if mv else 1.0)
            viewed[bool(r["voice"])].append((r["avg_view_pct"] or 0) / mp if mp else 1.0)
        channels_both.append({"channel": ch, "voiced": len(voiced), "plain": len(plain),
                              "voiced_views": median(r["views"] or 0 for r in voiced),
                              "plain_views": median(r["views"] or 0 for r in plain)})
    groups = [{"voiced": v, "shorts": len(views[v]), "views": median(views[v]), "viewed": median(viewed[v])}
              for v in (True, False) if views[v]]
    return {"groups": groups, "channels": channels_both, "one_kind": one_kind}


def review_rates(c: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Per channel, Shorts reviewed (made in the last `days` days): how many were rejected, and why."""
    reviewed = ("rejected", "approved", "scheduled", "uploaded")
    out = []
    for ch, total, rejected in c.execute(
            f"""SELECT channel, COUNT(*), SUM(status = 'rejected') FROM shorts
                 WHERE status IN ({','.join('?' * len(reviewed))}) AND NOT stats_hidden
                   AND created_at >= datetime('now', ?)
                 GROUP BY channel ORDER BY SUM(status = 'rejected') * 1.0 / COUNT(*) DESC""",
            (*reviewed, f"-{days} days")).fetchall():
        reasons: dict[str, int] = {}
        for (reason,) in c.execute("""SELECT reject_reason FROM shorts WHERE channel = ? AND status = 'rejected'
                                        AND created_at >= datetime('now', ?)""", (ch, f"-{days} days")):
            key = (reason or "no reason").split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
        out.append({"channel": ch, "reviewed": total, "rejected": rejected, "rate": rejected / total,
                    "reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:3]})
    return out


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
    from ..timing import claude_usage, summary
    return render_template("stats.html", compare=compare_channels(c, days, csort), days=days, ages=AGES,
                           csort=csort, compare_sorts=COMPARE_SORTS, rows=rows, totals=totals, by_format=by_format, voice=voice_test(c, days), reviews=review_rates(c), sort=sort,
                           channels=channels(), selected=selected, timings=summary(c),
                           claude=claude_usage(c))


@bp.post("/stats/fetch-all")
def fetch_all():
    from .jobs import start_job
    return start_job(None, ["stats", "--all"], "Fetch stats, all channels")
