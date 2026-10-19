"""Step 8: stats straight from YouTube. Every fetch reads each channel's whole uploads list from the
Data API (live views, likes, state: live / scheduled / private / gone), then one Analytics report per
channel (engaged views, avg viewed, subs), and stores it all in yt_videos. The Stats page reads
that table, so it shows what is on the channel now, not what the pipeline remembers.

Analytics lags 1-3 days: a live video missing from the report is "not in Analytics yet" (NULL,
shown as '–') while it is younger than LAG_DAYS, and 0 after that."""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .config import Channel, Settings

log = logging.getLogger(__name__)

AGES = (1, 3, 7)  # stats_age: views etc. in a Short's first 1, 3 and 7 days
LAG_DAYS = 3      # YouTube Analytics numbers for a day are final about this many days later
SHORT_MAX_S = 180  # YouTube Shorts are at most 3 minutes
METRICS = "views,engagedViews,averageViewPercentage,averageViewDuration,likes,shares,subscribersGained"
RETRIES = 3       # googleapiclient retries 5xx and rate limits with back-off; never quota-exceeded


def _exec(request):
    return request.execute(num_retries=RETRIES)


def parse_time(s: str | None) -> datetime | None:
    """'2026-10-19T15:00:00Z', '2026-10-19T15:00:00.000Z' or '2026-10-19 15:00:00' -> aware UTC."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "").replace(" ", "T")[:19]).replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def duration_s(iso: str | None) -> float | None:
    """ISO 8601 duration ('PT1M5S', 'P0D') -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", iso or "")
    if not m:
        return None
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def state_of(item: dict, now: datetime) -> str:
    st = item.get("status", {})
    if st.get("uploadStatus") in ("failed", "rejected", "deleted"):
        return "failed"
    if st.get("privacyStatus") == "private":
        when = parse_time(st.get("publishAt"))
        return "scheduled" if when and when > now else "private"
    if st.get("uploadStatus") == "uploaded":  # still processing
        return "processing"
    return "live"  # public or unlisted


def channel_videos(yt, known: list[str]) -> tuple[str, dict[str, dict]]:
    """(YouTube channel id, {video id: videos.list item}) for every upload on the logged-in channel,
    plus the ids we know from our own database (so scheduled ones are covered even if the uploads
    playlist leaves them out). Ids YouTube doesn't return are gone."""
    ch = _exec(yt.channels().list(part="id,contentDetails", mine=True)).get("items") or []
    if not ch:
        raise RuntimeError("this YouTube login has no channel")
    uploads = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while True:
        res = _exec(yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50,
                                            pageToken=token))
        ids += [it["contentDetails"]["videoId"] for it in res.get("items", [])]
        token = res.get("nextPageToken")
        if not token:
            break
    want = list(dict.fromkeys(ids + known))
    items = {}
    for i in range(0, len(want), 50):
        res = _exec(yt.videos().list(part="snippet,status,statistics,contentDetails", id=",".join(want[i:i + 50]),
                                     maxResults=50))
        items.update({it["id"]: it for it in res.get("items", [])})
    return ch[0]["id"], items


def channel_analytics(yta, start: date, end: date) -> dict[str, list]:
    """The 'top videos' report: one row per video with views in the period, 200 per page (the most
    the API allows); sort is required for it."""
    out, index = {}, 1
    while True:
        res = _exec(yta.reports().query(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                                        metrics=METRICS, dimensions="video", sort="-views", maxResults=200,
                                        startIndex=index))
        rows = res.get("rows") or []
        out.update({r[0]: list(r[1:]) for r in rows})
        if len(rows) < 200:
            return out
        index += 200


def analytics_through(yta, today: date) -> str | None:
    """Last day the channel has any Analytics data for (so the page can say how far behind it is)."""
    res = _exec(yta.reports().query(ids="channel==MINE", startDate=(today - timedelta(days=14)).isoformat(),
                                    endDate=today.isoformat(), metrics="views", dimensions="day", sort="day"))
    rows = res.get("rows") or []
    return rows[-1][0] if rows else None


def _video_report(yta, youtube_id: str, start: str, end: str) -> list:
    res = _exec(yta.reports().query(ids="channel==MINE", startDate=start, endDate=end, metrics=METRICS,
                                    filters=f"video=={youtube_id}"))
    return (res.get("rows") or [[0] * len(METRICS.split(","))])[0]


def sync_channel(conn: sqlite3.Connection, settings: Settings, channel: Channel, interactive: bool = True,
                 now: datetime | None = None) -> int:
    """Fetch everything for one channel and store it in one go. Returns the number of live Shorts.
    A failure is recorded in yt_sync (shown on Stats) and raised; the last good numbers stay."""
    try:
        return _sync(conn, settings, channel, interactive, now or datetime.now(timezone.utc))
    except Exception as e:
        conn.rollback()
        conn.execute("""INSERT INTO yt_sync (channel, error, error_at) VALUES (?, ?, datetime('now'))
                        ON CONFLICT(channel) DO UPDATE SET error=excluded.error, error_at=excluded.error_at""",
                     (channel.id, str(e)[:500]))
        conn.commit()
        raise


def _sync(conn: sqlite3.Connection, settings: Settings, channel: Channel, interactive: bool, now: datetime) -> int:
    from googleapiclient.discovery import build

    from . import upload

    creds = upload.credentials(settings, channel, interactive)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    yta = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    today = now.date()
    ours = {r["youtube_id"]: r for r in conn.execute(
        "SELECT id, youtube_id, publish_at, status FROM shorts WHERE channel=? AND youtube_id IS NOT NULL", (channel.id,))}
    yt_channel, items = channel_videos(yt, list(ours))
    if channel.youtube_channel_id and yt_channel != channel.youtube_channel_id:
        raise RuntimeError(f"the YouTube login for '{channel.name}' is for another channel ({yt_channel}): "
                           "use Connect YouTube on its Settings page")

    videos = []
    for vid, it in items.items():
        st, sn = it.get("status", {}), it.get("snippet", {})
        own = ours.get(vid)
        state = state_of(it, now)
        # publishAt only stays set while a video is scheduled; after that our own time, else upload time
        when = st.get("publishAt") or (own["publish_at"] if own and own["publish_at"] else sn.get("publishedAt"))
        dur = duration_s(it.get("contentDetails", {}).get("duration"))
        stats = it.get("statistics", {})
        live = state == "live"
        videos.append({
            "youtube_id": vid, "channel": channel.id, "short_id": own["id"] if own else None,
            "title": sn.get("title"), "state": state, "privacy": st.get("privacyStatus"),
            "publish_at": _iso(parse_time(when)) if when else None, "duration_s": dur,
            "is_short": int(dur is None or dur <= SHORT_MAX_S),
            "views": int(stats.get("viewCount", 0)) if live else None,
            "likes": int(stats.get("likeCount", 0)) if live else None,
            "comments": int(stats.get("commentCount", 0)) if live else None,
        })

    analytics, through = {}, None
    live = [v for v in videos if v["state"] == "live"]
    if live:
        start = min((parse_time(v["publish_at"]) or now).date() for v in live)
        analytics = channel_analytics(yta, min(start, today), today)
        through = analytics_through(yta, today)
    for v in live:
        row = analytics.get(v["youtube_id"])
        if row is None:
            age = (today - (parse_time(v["publish_at"]) or now).date()).days
            row = [None] * 7 if age < LAG_DAYS else [0, 0, 0.0, 0.0, 0, 0, 0]
        v.update(zip(("an_views", "engaged_views", "avg_view_pct", "avg_view_dur", "an_likes", "shares",
                      "subs_gained"), row))

    # Each Short's first 1, 3 and 7 days, once they are final (these few per-video queries happen once).
    ages = []
    for v in live:
        if not v["short_id"] or not v["publish_at"]:
            continue
        start = parse_time(v["publish_at"]).date()
        have = {x[0] for x in conn.execute("SELECT days FROM stats_age WHERE short_id=?", (v["short_id"],))}
        for days in AGES:
            end = start + timedelta(days=days - 1)
            if days not in have and end + timedelta(days=LAG_DAYS) <= today:
                ages.append((v["short_id"], days, *_video_report(yta, v["youtube_id"], start.isoformat(), end.isoformat())))

    # Everything is fetched: write it all at once.
    seen = _iso(now)
    with conn:
        for v in videos:
            an = {k: v.get(k) for k in ("an_views", "engaged_views", "avg_view_pct", "avg_view_dur", "shares", "subs_gained")}
            conn.execute(
                """INSERT INTO yt_videos (youtube_id, channel, short_id, title, state, privacy, publish_at, duration_s,
                                          is_short, views, likes, comments, an_views, engaged_views, avg_view_pct,
                                          avg_view_dur, shares, subs_gained, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(youtube_id) DO UPDATE SET channel=excluded.channel, short_id=excluded.short_id,
                       title=excluded.title, state=excluded.state, privacy=excluded.privacy,
                       publish_at=excluded.publish_at, duration_s=excluded.duration_s, is_short=excluded.is_short,
                       views=excluded.views, likes=excluded.likes, comments=excluded.comments,
                       an_views=excluded.an_views, engaged_views=excluded.engaged_views,
                       avg_view_pct=excluded.avg_view_pct, avg_view_dur=excluded.avg_view_dur,
                       shares=excluded.shares, subs_gained=excluded.subs_gained, last_seen=excluded.last_seen""",
                (v["youtube_id"], v["channel"], v["short_id"], v["title"], v["state"], v["privacy"], v["publish_at"],
                 v["duration_s"], v["is_short"], v["views"], v["likes"], v["comments"], *an.values(), seen))
            if v["state"] == "live":
                conn.execute("INSERT OR REPLACE INTO yt_daily (youtube_id, day, views) VALUES (?,?,?)",
                             (v["youtube_id"], today.isoformat(), v["views"]))
                if v["short_id"]:
                    # the pipeline's own record: published now, and a stats row (hero.py learns from it)
                    conn.execute("UPDATE shorts SET status='published' WHERE id=? AND status IN ('scheduled', 'uploaded')",
                                 (v["short_id"],))
                    conn.execute(
                        """INSERT INTO stats (short_id, views, engaged_views, avg_view_pct, avg_view_dur, likes, shares,
                                              subs_gained, live_views, live_likes) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (v["short_id"], an["an_views"], an["engaged_views"], an["avg_view_pct"], an["avg_view_dur"],
                         v.get("an_likes"), an["shares"], an["subs_gained"], v["views"], v["likes"]))
        # known before, not on YouTube any more: deleted
        gone = [vid for vid in ours if vid not in items]
        conn.execute(f"""UPDATE yt_videos SET state='gone', views=NULL, likes=NULL, comments=NULL
                          WHERE channel=? AND youtube_id NOT IN ({','.join('?' * len(items))})""",
                     (channel.id, *items))
        for vid in gone:
            conn.execute("""INSERT OR IGNORE INTO yt_videos (youtube_id, channel, short_id, title, state, publish_at, last_seen)
                            VALUES (?,?,?,(SELECT title FROM shorts WHERE id=?),'gone',?,?)""",
                         (vid, channel.id, ours[vid]["id"], ours[vid]["id"], ours[vid]["publish_at"], seen))
        conn.executemany("""INSERT OR IGNORE INTO stats_age (short_id, days, views, engaged_views, avg_view_pct,
                            avg_view_dur, likes, shares, subs_gained) VALUES (?,?,?,?,?,?,?,?,?)""", ages)
        # rows an older version saved for Shorts that weren't public yet (all zeros)
        conn.execute("""DELETE FROM stats WHERE short_id IN (SELECT id FROM shorts WHERE channel=? AND publish_at IS NOT NULL)
                          AND fetched_at < replace(substr((SELECT publish_at FROM shorts WHERE id=short_id), 1, 19), 'T', ' ')""",
                     (channel.id,))
        conn.execute("""INSERT INTO yt_sync (channel, ok_at, error, error_at, analytics_through, yt_channel_id)
                        VALUES (?, datetime('now'), NULL, NULL, ?, ?)
                        ON CONFLICT(channel) DO UPDATE SET ok_at=excluded.ok_at, error=NULL, error_at=NULL,
                            analytics_through=COALESCE(excluded.analytics_through, yt_sync.analytics_through),
                            yt_channel_id=excluded.yt_channel_id""",
                     (channel.id, through, yt_channel))
    return sum(1 for v in live if v["is_short"])
