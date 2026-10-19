"""Stats: views and retention per Short and per channel, from the 'stats' table (python -m shorts stats)."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import median

from flask import Blueprint, redirect, render_template, request

from ..upload import AGES, LAG_DAYS
from .common import channels, conn

bp = Blueprint("stats", __name__)

# Per video, from yt_videos (ytstats.py): views/likes are live; engaged is engaged views / Analytics views.
SORTS = {"views": "views DESC", "retention": "avg_view_pct DESC", "engaged": "engaged_views * 1.0 / NULLIF(an_views, 0) DESC",
         "newest": "publish_at DESC"}
VIDEOS = """SELECT v.*, s.voice, s.format_id FROM yt_videos v LEFT JOIN shorts s ON s.id = v.short_id
             WHERE v.is_short AND (? IS NULL OR v.channel = ?)"""
NOT_LIVE = {"scheduled": "Scheduled", "private": "Private", "processing": "Processing", "failed": "Failed", "gone": "Deleted"}


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


def ago(when: str | None, now: datetime | None = None) -> str:
    """'2026-10-19 13:43:41' (UTC) -> '5 min ago'."""
    if not when:
        return "never"
    mins = int(((now or datetime.now(timezone.utc)) - datetime.fromisoformat(when.replace("Z", "").replace(" ", "T")[:19])
                .replace(tzinfo=timezone.utc)).total_seconds() // 60)
    return ("just now" if mins < 1 else f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago" if mins < 48 * 60
            else f"{mins // 1440} days ago")


def channel_cards(c: sqlite3.Connection, selected: str | None) -> list[dict]:
    """Per channel what is on YouTube now, its numbers, and how the last fetch went."""
    totals = {r["channel"]: dict(r) for r in c.execute(
        """SELECT channel, SUM(state = 'live' AND NOT hidden) AS live, SUM(state = 'scheduled') AS scheduled,
                  MIN(CASE WHEN state = 'scheduled' THEN publish_at END) AS next_at, SUM(state = 'gone') AS gone,
                  SUM(CASE WHEN state = 'live' AND NOT hidden THEN views END) AS views,
                  SUM(CASE WHEN state = 'live' AND NOT hidden THEN engaged_views END) * 1.0
                      / NULLIF(SUM(CASE WHEN state = 'live' AND NOT hidden AND engaged_views IS NOT NULL THEN an_views END), 0) AS engaged,
                  AVG(CASE WHEN state = 'live' AND NOT hidden THEN avg_view_pct END) AS pct,
                  SUM(CASE WHEN state = 'live' AND NOT hidden THEN subs_gained END) AS subs,
                  SUM(state = 'live' AND NOT hidden AND engaged_views IS NULL) AS waiting
             FROM yt_videos WHERE is_short GROUP BY channel""")}
    sync = {r["channel"]: dict(r) for r in c.execute("SELECT * FROM yt_sync")}
    out = []
    for ch in channels():
        if selected and ch.id != selected or (ch.id not in totals and ch.id not in sync):
            continue
        t = totals.get(ch.id) or dict.fromkeys(("live", "scheduled", "next_at", "gone", "views", "engaged", "pct", "subs",
                                                 "waiting"))
        sy = sync.get(ch.id, {})
        out.append({"channel": ch.id, "name": ch.name, **t, "ok": ago(sy.get("ok_at")), "error": sy.get("error"),
                    "error_at": ago(sy.get("error_at")) if sy.get("error") else None,
                    "through": sy.get("analytics_through")})
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
    sort = request.args.get("sort", "engaged")  # views count every replay since Mar 2025: judge by engaged
    selected = request.args.get("channel") or None
    rows = c.execute(f"{VIDEOS} AND v.state = 'live' AND NOT v.hidden ORDER BY {SORTS.get(sort, SORTS['views'])} NULLS LAST",
                     (selected, selected)).fetchall()
    not_live = c.execute(f"""{VIDEOS} AND v.state != 'live' AND NOT v.hidden
                             ORDER BY v.state != 'scheduled', CASE WHEN v.state = 'scheduled' THEN v.publish_at END,
                                      v.publish_at DESC LIMIT 100""", (selected, selected)).fetchall()
    hidden = c.execute(f"{VIDEOS} AND v.hidden ORDER BY v.publish_at DESC", (selected, selected)).fetchall()
    by_format = c.execute("""SELECT v.channel, s.format_id, COUNT(*) AS shorts, AVG(v.views) AS views, AVG(v.avg_view_pct) AS pct
                               FROM yt_videos v JOIN shorts s ON s.id = v.short_id
                              WHERE v.state = 'live' AND NOT v.hidden AND v.is_short AND (? IS NULL OR v.channel = ?)
                              GROUP BY v.channel, s.format_id ORDER BY v.channel, views DESC""", (selected, selected)).fetchall()
    from ..timing import claude_usage, summary
    return render_template("stats.html", compare=compare_channels(c, days, csort), days=days, ages=AGES,
                           csort=csort, compare_sorts=COMPARE_SORTS, rows=rows, cards=channel_cards(c, selected),
                           not_live=not_live, not_live_names=NOT_LIVE, hidden=hidden, by_format=by_format,
                           voice=voice_test(c, days), reviews=review_rates(c), sort=sort,
                           channels=channels(), selected=selected, timings=summary(c),
                           claude=claude_usage(c))


@bp.post("/stats/hide/<youtube_id>")
def hide(youtube_id: str):
    """Hide button: leave a video out of Stats (also the same-age comparison and the voice test)."""
    c = conn()
    on = 1 if request.form.get("hidden", "1") == "1" else 0
    c.execute("UPDATE yt_videos SET hidden=? WHERE youtube_id=?", (on, youtube_id))
    c.execute("UPDATE shorts SET stats_hidden=? WHERE youtube_id=?", (on, youtube_id))
    c.commit()
    return redirect(request.referrer or "/stats")


@bp.post("/stats/fetch-all")
def fetch_all():
    from .jobs import start_job
    return start_job(None, ["stats", "--all"], "Fetch stats, all channels")
