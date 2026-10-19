"""Upload approved Shorts as private with a publishAt time, so YouTube publishes them on schedule
(the laptop doesn't need to be on at publish time). OAuth token per channel."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
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


class LoginExpired(RuntimeError):
    """The saved YouTube login no longer works and nobody is there to log in again."""


def login_expired_path(settings: Settings, channel_id: str) -> Path:
    return settings.data_dir / f"login-expired-{channel_id}.txt"


def credentials(settings: Settings, channel: Channel, interactive: bool = True):
    """interactive=False (daily upload, stats for all channels): never open a browser to log in,
    raise LoginExpired instead so a job nobody watches doesn't wait forever."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = settings.secrets_dir / f"token-{channel.id}.json"
    expired = login_expired_path(settings, channel.id)
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # Expired or revoked (in Testing mode Google drops refresh tokens after 7 days): log in again.
            log.warning("saved login for '%s' no longer works (%s); logging in again", channel.name, e)
            expired.write_text(iso(datetime.now(timezone.utc)), encoding="utf-8")  # warning on Home
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise LoginExpired(f"the YouTube login for '{channel.name}' has expired: use Connect YouTube "
                               "on its Settings page")
        flow = InstalledAppFlow.from_client_secrets_file(str(_client_secret(settings)), SCOPES)
        print(f"Log in with the Google account that owns the '{channel.name}' channel.")
        if _headless():
            # Server without a browser: Google sends the login back to localhost:8765 on the server,
            # so open the printed link on the laptop while `ssh -L 8765:localhost:8765 <server>` runs.
            # Or connect on the laptop and copy secrets/token-<channel>.json to the server.
            print("No browser here. On your laptop run: ssh -L 8765:localhost:8765 <server>, then open "
                  "the link below in the laptop's browser.", flush=True)
            creds = flow.run_local_server(port=8765, open_browser=False)
        else:
            creds = flow.run_local_server(port=0)
    token.write_text(creds.to_json(), encoding="utf-8")
    expired.unlink(missing_ok=True)
    return creds


def _headless() -> bool:
    """A Linux server: no desktop to open a browser on."""
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def youtube(settings: Settings, channel: Channel, interactive: bool = True):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=credentials(settings, channel, interactive), cache_discovery=False)


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
    """A publish time per Short (in the order given). Every free slot is filled, earliest first:
    each slot gets the first waiting Short that is at least settings.sibling_gap_days (calendar
    days) away from the other Shorts from the same video on this channel; when none is, the slot
    still gets the first waiting Short, so the channel never has empty slots."""
    if not short_ids:
        return []
    gap = settings.sibling_gap_days
    free = next_slots(conn, channel, len(short_ids), now=now)
    taken: dict[int, list] = {}  # video -> publish dates of its Shorts on this channel
    for r in conn.execute(
            """SELECT sc.asset_id, s.publish_at FROM shorts s JOIN scenes sc ON sc.id = s.hero_scene_id
                WHERE s.publish_at IS NOT NULL AND s.status != 'rejected' AND s.channel = ?""", (channel.id,)):
        taken.setdefault(r["asset_id"], []).append(datetime.fromisoformat(r["publish_at"].replace("Z", "+00:00")).date())
    video = {}
    for sid in short_ids:
        a = conn.execute("""SELECT sc.asset_id FROM shorts s JOIN scenes sc ON sc.id = s.hero_scene_id
                             WHERE s.id=?""", (sid,)).fetchone()
        video[sid] = a[0] if a else -sid
    waiting, out = list(short_ids), {}
    for slot in free:
        pick = next((sid for sid in waiting
                     if all(abs((slot.date() - d).days) >= gap for d in taken.get(video[sid], []))), waiting[0])
        waiting.remove(pick)
        taken.setdefault(video[pick], []).append(slot.date())
        out[pick] = slot
    return [out[sid] for sid in short_ids]


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


def channel_uploads_last_day(conn: sqlite3.Connection, channel: Channel, now: datetime | None = None) -> int:
    """Uploads to this channel in the last 24 hours (YouTube's per-channel limit is a rolling window)."""
    now = now or datetime.now(timezone.utc)
    return conn.execute("SELECT COUNT(*) FROM shorts WHERE channel=? AND uploaded_at >= ?",
                        (channel.id, iso(now - timedelta(days=1)))).fetchone()[0]


def _blocked_path(settings: Settings, channel: Channel) -> Path:
    return settings.data_dir / f"upload-blocked-{channel.id}.txt"


def blocked_until(settings: Settings, channel: Channel, now: datetime | None = None) -> datetime | None:
    """When YouTube refused an upload for this channel (uploadLimitExceeded), don't try again for 24 hours."""
    now = now or datetime.now(timezone.utc)
    try:
        since = datetime.fromisoformat(_blocked_path(settings, channel).read_text(encoding="utf-8").strip()
                                       .replace("Z", "+00:00"))
    except (OSError, ValueError):
        return None
    until = since + timedelta(days=1)
    return until if until > now else None


def _limit_reason(e: Exception) -> str | None:
    """'uploadLimitExceeded' (channel's daily limit) or 'quotaExceeded' (API quota), else None."""
    content = getattr(e, "content", b"") or b""
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    for reason in ("uploadLimitExceeded", "quotaExceeded"):
        if reason in text or reason in str(e):
            return reason
    return None


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
                    limit: int | None = None, schedule: bool = True, interactive: bool = True) -> list[str]:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    from .render import metadata

    rows = conn.execute("SELECT * FROM shorts WHERE channel=? AND status='approved' ORDER BY id",
                        (channel.id,)).fetchall()
    if limit is not None:
        rows = rows[:limit]
    until = blocked_until(settings, channel)
    if until:
        log.warning("'%s': YouTube refused uploads (daily limit); trying again after %s", channel.id, iso(until))
        return []
    per_channel = settings.channel_uploads_per_day - channel_uploads_last_day(conn, channel)
    if len(rows) > per_channel:
        log.info("'%s': %d of %d uploads used in the last 24 hours; uploading %d of %d now, the rest stay "
                 "approved for the next run", channel.id, settings.channel_uploads_per_day - per_channel,
                 settings.channel_uploads_per_day, max(per_channel, 0), len(rows))
        rows = rows[:max(per_channel, 0)]
    left = settings.upload_quota - uploads_today(conn)
    if len(rows) > left:
        log.warning("daily upload quota: %d of %d used today (all channels); uploading %d of %d now, "
                    "the rest tomorrow", settings.upload_quota - left, settings.upload_quota, max(left, 0), len(rows))
        rows = rows[:max(left, 0)]
    if not rows:
        return []
    yt = youtube(settings, channel, interactive)
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
        try:
            while response is None:
                _, response = request.next_chunk()
        except HttpError as e:
            reason = _limit_reason(e)
            if not reason:
                raise
            if reason == "uploadLimitExceeded":
                _blocked_path(settings, channel).write_text(iso(datetime.now(timezone.utc)), encoding="utf-8")
                log.warning("'%s': YouTube's daily upload limit for this channel is reached; stopped after %d. "
                            "The other %d stay approved and upload on a later run.",
                            channel.id, len(done), len(rows) - len(done))
            else:
                log.warning("'%s': the YouTube API quota for today is used up; stopped after %d. "
                            "The other %d stay approved.", channel.id, len(done), len(rows) - len(done))
            break
        vid = response["id"]
        db.set_short(conn, s["id"], status="scheduled" if slot else "uploaded", youtube_id=vid,
                     publish_at=iso(slot) if slot else None, uploaded_at=iso(datetime.now(timezone.utc)))
        log.info("uploaded short %s as https://youtu.be/%s (publish %s)", s["id"], vid, iso(slot) if slot else "private")
        done.append(vid)
    return done


UPLOADS_AT_ONCE = 3  # channels uploading at the same time (each upload is many 8 MB round trips)
STATS_AT_ONCE = 4    # channels fetching stats at the same time


def _per_channel(settings: Settings, cids: list[str], work, at_once: int, failed: str = "failed") -> dict[str, str]:
    """Run work(conn, channel) for each channel id, `at_once` channels at a time, each in a thread
    with its own database connection. One channel's problem doesn't stop the others."""
    from concurrent.futures import ThreadPoolExecutor

    from .config import load_channel

    def one(cid: str) -> str:
        c = db.connect(settings.db_path)
        try:
            return work(c, load_channel(cid, settings.config_dir))
        except Exception as e:  # noqa: BLE001
            log.warning("%s: %s: %s", cid, failed, e)
            return f"{failed}: {e}"
        finally:
            c.close()

    if not cids:
        return {}
    with ThreadPoolExecutor(max_workers=min(at_once, len(cids)), thread_name_prefix="yt") as pool:
        out = dict(zip(cids, pool.map(one, cids)))
    for cid, result in out.items():
        log.info("%s: %s", cid, result)
    return out


def upload_all(conn: sqlite3.Connection, settings: Settings) -> dict[str, str]:
    """'Daily upload': every channel uploads its approved Shorts, up to channel_uploads_per_day,
    then fetches stats. Several channels upload at once; the shared API quota is divided over them
    first (in channel order), so together they never go over it."""
    from .config import list_channels, load_channel

    left = settings.upload_quota - uploads_today(conn)
    share: dict[str, int] = {}
    for cid in list_channels(settings.config_dir):
        ch = load_channel(cid, settings.config_dir)
        approved = conn.execute("SELECT COUNT(*) FROM shorts WHERE channel=? AND status='approved'",
                                (cid,)).fetchone()[0]
        n = max(0, min(approved, settings.channel_uploads_per_day - channel_uploads_last_day(conn, ch), left))
        share[cid] = n
        left -= n

    def upload(c: sqlite3.Connection, ch: Channel) -> str:
        return f"uploaded {len(upload_approved(c, settings, ch, limit=share[ch.id], interactive=False))}"

    out = _per_channel(settings, list(share), upload, UPLOADS_AT_ONCE)
    for cid, result in fetch_stats_all(conn, settings).items():
        out[cid] = f"{out.get(cid, '')}, {result}".lstrip(", ")
    return out


from .ytstats import AGES, LAG_DAYS  # noqa: E402,F401 - Stats page imports them from here


def fetch_stats(conn: sqlite3.Connection, settings: Settings, channel: Channel, interactive: bool = True) -> int:
    """Step 8: every video on the channel from YouTube, with live views and Analytics (ytstats.py).
    Returns the number of live Shorts."""
    from .ytstats import sync_channel
    return sync_channel(conn, settings, channel, interactive)


def fetch_stats_all(conn: sqlite3.Connection, settings: Settings) -> dict[str, str]:
    """Stats for every channel that has a YouTube login (several at once); never opens a browser."""
    from .config import list_channels

    cids = [cid for cid in list_channels(settings.config_dir)
            if (settings.secrets_dir / f"token-{cid}.json").exists()]
    return _per_channel(settings, cids, lambda c, ch: f"stats for {fetch_stats(c, settings, ch, interactive=False)}",
                        STATS_AT_ONCE, failed="stats failed")


def save_body_preview(path: Path, meta: dict, publish_at: datetime | None) -> None:
    path.write_text(json.dumps(body_for(meta, publish_at), indent=2, ensure_ascii=False), encoding="utf-8")
