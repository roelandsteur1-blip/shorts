"""Upload approved Shorts as private with a publishAt time, so YouTube publishes them on schedule
(the laptop doesn't need to be on at publish time). OAuth token per channel."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from . import db
from .config import Channel, Settings

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def _client_secret(settings: Settings) -> Path:
    path = settings.secrets_dir / "client_secret.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Put the OAuth client file from Google Cloud (Desktop app) at {path}")
    return path


def credentials(settings: Settings, channel: Channel):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = settings.secrets_dir / f"token-{channel.id}.json"
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # Expired or revoked (in Testing mode Google drops refresh tokens after 7 days): log in again.
            log.warning("saved login for '%s' no longer works (%s); logging in again", channel.name, e)
            creds = None
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(_client_secret(settings)), SCOPES)
        print(f"Log in with the Google account that owns the '{channel.name}' channel.")
        creds = flow.run_local_server(port=0)
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def youtube(settings: Settings, channel: Channel):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=credentials(settings, channel), cache_discovery=False)


def my_channel(yt) -> dict:
    """The YouTube channel the saved login belongs to: {id, title, thumbnail}."""
    res = yt.channels().list(part="snippet", mine=True).execute()
    items = res.get("items") or []
    if not items:
        raise RuntimeError("this Google account has no YouTube channel (create one first)")
    sn = items[0]["snippet"]
    thumb = ((sn.get("thumbnails") or {}).get("default") or {}).get("url")
    return {"id": items[0]["id"], "title": sn.get("title"), "thumbnail": thumb}


def found_path(settings: Settings, channel_id: str) -> Path:
    return settings.secrets_dir / f"channel-{channel_id}.json"


def connect(settings: Settings, channel: Channel, fresh: bool = False) -> dict:
    """'Connect YouTube': log in (browser on this laptop), then look up which channel that login
    owns and save it, so the dashboard can ask "is this the right channel?"."""
    token = settings.secrets_dir / f"token-{channel.id}.json"
    if fresh and token.exists():
        token.unlink()
    found = my_channel(youtube(settings, channel))
    found_path(settings, channel.id).write_text(json.dumps(found, indent=2), encoding="utf-8")
    return found


def check_channel(yt, channel: Channel) -> None:
    """Refuse to upload when the saved login belongs to another channel than the one confirmed on
    'Connect YouTube' (several Brand Accounts share one Google login)."""
    if not channel.youtube_channel_id:
        log.warning("'%s' has no confirmed YouTube channel yet: use Connect YouTube on its dashboard "
                    "page so uploads can't go to the wrong channel", channel.id)
        return
    found = my_channel(yt)
    if found["id"] != channel.youtube_channel_id:
        raise RuntimeError(f"the saved login is for YouTube channel '{found['title']}' ({found['id']}), "
                           f"not the one connected to '{channel.id}' ({channel.youtube_channel_id}): "
                           "connect YouTube again for this channel")


def next_slots(conn: sqlite3.Connection, channel: Channel, n: int,
               now: datetime | None = None) -> list[datetime]:
    """Next n free publish times: channel.publish_times_utc each day, at most uploads_per_day,
    after anything already scheduled, and at least one hour from now."""
    now = now or datetime.now(timezone.utc)
    taken = {r[0] for r in conn.execute(
        "SELECT publish_at FROM shorts WHERE channel=? AND publish_at IS NOT NULL", (channel.id,))}
    times = sorted(time.fromisoformat(t) for t in channel.publish_times_utc)[: channel.uploads_per_day]
    slots, day = [], now.date()
    while len(slots) < n:
        for t in times:
            slot = datetime.combine(day, t, tzinfo=timezone.utc)
            if slot >= now + timedelta(hours=1) and iso(slot) not in taken:
                slots.append(slot)
                if len(slots) == n:
                    break
        day += timedelta(days=1)
    return slots


def slots_for(conn: sqlite3.Connection, settings: Settings, channel: Channel, short_ids: list[int],
              now: datetime | None = None) -> list[datetime]:
    """A publish time per Short, in order: the earliest free slot that is at least
    settings.sibling_gap_days (calendar days) away from every other Short from the same video,
    scheduled before or in this same run. Skipped slots stay free for the next Short."""
    if not short_ids:
        return []
    gap = settings.sibling_gap_days
    per_day = max(1, min(channel.uploads_per_day, len(channel.publish_times_utc)))
    free = next_slots(conn, channel, len(short_ids) * (gap + 1) * per_day + per_day, now=now)
    taken: dict[int, list] = {}  # video -> publish dates of its Shorts
    for r in conn.execute(
            """SELECT sc.asset_id, s.publish_at FROM shorts s JOIN scenes sc ON sc.id = s.hero_scene_id
                WHERE s.publish_at IS NOT NULL AND s.status != 'rejected'"""):
        taken.setdefault(r["asset_id"], []).append(datetime.fromisoformat(r["publish_at"].replace("Z", "+00:00")).date())
    out = []
    for sid in short_ids:
        asset = conn.execute("""SELECT sc.asset_id FROM shorts s JOIN scenes sc ON sc.id = s.hero_scene_id
                                 WHERE s.id=?""", (sid,)).fetchone()
        dates = taken.setdefault(asset[0] if asset else -sid, [])
        pick = next((s for s in free if all(abs((s.date() - d).days) >= gap for d in dates)), free[-1])
        free.remove(pick)
        dates.append(pick.date())
        out.append(pick)
    return out


def pacific_midnight(now: datetime | None = None) -> datetime:
    """Start of today in US Pacific time, as UTC: the YouTube API quota resets then. DST rule
    inline (second Sunday of March to first Sunday of November), so no tz database is needed."""
    now = now or datetime.now(timezone.utc)

    def nth_sunday(year: int, month: int, n: int) -> datetime:
        d = datetime(year, month, 1, tzinfo=timezone.utc)
        d += timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))
        return d

    y = now.year
    dst = nth_sunday(y, 3, 2) + timedelta(hours=10) <= now < nth_sunday(y, 11, 1) + timedelta(hours=9)
    offset = timedelta(hours=-7 if dst else -8)
    local = now + offset
    return datetime(local.year, local.month, local.day, tzinfo=timezone.utc) - offset


def uploads_today(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Uploads since the quota reset, all channels together (they share one Google Cloud project)."""
    return conn.execute("SELECT COUNT(*) FROM shorts WHERE uploaded_at >= ?",
                        (iso(pacific_midnight(now)),)).fetchone()[0]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def body_for(meta: dict, publish_at: datetime | None) -> dict:
    status = {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": meta.get("containsSyntheticMedia", False),
    }
    if publish_at:
        status["publishAt"] = iso(publish_at)
    return {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description"],
            "tags": meta.get("tags", [])[:15],
            "categoryId": meta.get("categoryId", "28"),
        },
        "status": status,
    }


def upload_approved(conn: sqlite3.Connection, settings: Settings, channel: Channel,
                    limit: int | None = None, schedule: bool = True) -> list[str]:
    from googleapiclient.http import MediaFileUpload

    from .render import metadata

    rows = conn.execute("SELECT * FROM shorts WHERE channel=? AND status='approved' ORDER BY id",
                        (channel.id,)).fetchall()
    if limit is not None:
        rows = rows[:limit]
    left = settings.upload_quota - uploads_today(conn)
    if len(rows) > left:
        log.warning("daily upload quota: %d of %d used today (all channels); uploading %d of %d now, "
                    "the rest tomorrow", settings.upload_quota - left, settings.upload_quota, max(left, 0), len(rows))
        rows = rows[:max(left, 0)]
    if not rows:
        return []
    yt = youtube(settings, channel)
    check_channel(yt, channel)
    slots = slots_for(conn, settings, channel, [r["id"] for r in rows]) if schedule else [None] * len(rows)
    done = []
    for s, slot in zip(rows, slots):
        meta = metadata(conn, channel, s["id"])
        request = yt.videos().insert(
            part="snippet,status", body=body_for(meta, slot),
            media_body=MediaFileUpload(s["video_path"], mimetype="video/mp4", chunksize=8 << 20, resumable=True),
        )
        response = None
        while response is None:
            _, response = request.next_chunk()
        vid = response["id"]
        db.set_short(conn, s["id"], status="scheduled" if slot else "uploaded", youtube_id=vid,
                     publish_at=iso(slot) if slot else None, uploaded_at=iso(datetime.now(timezone.utc)))
        log.info("uploaded short %s as https://youtu.be/%s (publish %s)", s["id"], vid, iso(slot) if slot else "private")
        done.append(vid)
    return done


def fetch_stats(conn: sqlite3.Connection, settings: Settings, channel: Channel) -> int:
    """Step 8: pull per-video analytics. 'Viewed vs swiped' isn't in the API; engagedViews/views
    is the closest signal."""
    from googleapiclient.discovery import build

    creds = credentials(settings, channel)
    yta = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    rows = conn.execute(
        "SELECT id, youtube_id, publish_at, created_at FROM shorts WHERE channel=? AND youtube_id IS NOT NULL",
        (channel.id,)).fetchall()
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    for r in rows:
        start = (r["publish_at"] or r["created_at"])[:10]
        if start > today:
            continue
        res = yta.reports().query(
            ids="channel==MINE", startDate=start, endDate=today,
            metrics="views,engagedViews,averageViewPercentage,averageViewDuration,likes,shares,subscribersGained",
            filters=f"video=={r['youtube_id']}",
        ).execute()
        vals = (res.get("rows") or [[0, 0, 0, 0, 0, 0, 0]])[0]
        conn.execute(
            """INSERT INTO stats (short_id, views, engaged_views, avg_view_pct, avg_view_dur, likes, shares, subs_gained)
               VALUES (?,?,?,?,?,?,?,?)""", (r["id"], *vals))
        n += 1
    conn.commit()
    return n


def save_body_preview(path: Path, meta: dict, publish_at: datetime | None) -> None:
    path.write_text(json.dumps(body_for(meta, publish_at), indent=2, ensure_ascii=False), encoding="utf-8")
