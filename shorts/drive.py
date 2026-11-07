"""Upload via phone (research/app-music-workflow.md): for channels with upload_via = "phone" the
daily upload doesn't post to YouTube. It puts each approved Short (rendered without music) in
Google Drive, in "<drive_folder>/<channel name>/", with a .txt next to it holding the title,
description, tags and publish time. The Autosync app on Roeland's phone keeps that folder in sync
with a phone folder, so the Shorts show up in the gallery; he posts them in the YouTube app with a
trending sound ("Add sound") and schedules them there.

The stats sync (and check_all every 30 minutes from the dashboard) looks for new videos on the
channel whose title matches a Short waiting on Drive. A match gets the YouTube id and state like
an API upload (so stats and learning work), and its Drive files go to the Drive trash, which
Autosync mirrors by deleting them from the phone.

The Drive login uses scope drive.file: the app only sees the files and folders it made itself."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import Channel, Settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER = "application/vnd.google-apps.folder"
CHECK_EVERY = 30 * 60  # seconds between checks from the dashboard


def is_phone(channel: Channel) -> bool:
    return (channel.upload_via or "api") == "phone"


def token_path(settings: Settings) -> Path:
    return settings.secrets_dir / "token-drive.json"


def connected(settings: Settings) -> bool:
    return token_path(settings).exists()


class NotConnected(RuntimeError):
    """No (working) Drive login: use Connect Google Drive."""


def credentials(settings: Settings, interactive: bool = True):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    from . import upload

    token = token_path(settings)
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            log.warning("saved Google Drive login no longer works (%s)", e)
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise NotConnected("the Google Drive login is missing or expired: use Connect Google Drive")
        flow = InstalledAppFlow.from_client_secrets_file(str(upload._client_secret(settings)), SCOPES)
        print("Log in with the Google account whose Drive should get the Shorts for the phone.")
        if upload._headless():
            print("No browser here. On your laptop run: ssh -L 8765:localhost:8765 <server>, then open "
                  "the link below in the laptop's browser.", flush=True)
            creds = upload._login_on_port(flow, 8765)
        else:
            creds = flow.run_local_server(port=0)
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def service(settings: Settings, interactive: bool = False):
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=credentials(settings, interactive), cache_discovery=False)


def connect(settings: Settings) -> str:
    """'Connect Google Drive': log in, make the top folder, return its name."""
    token_path(settings).unlink(missing_ok=True)
    drv = service(settings, interactive=True)
    folder(drv, settings.drive_folder)
    return settings.drive_folder


# ---------- names ----------

def norm(title: str | None) -> str:
    """Titles compared loosely: case, punctuation, emoji and #hashtags don't count (the app may add
    a hashtag or Roeland may trim a character)."""
    t = re.sub(r"#\w+", " ", (title or "").lower())
    return re.sub(r"[^0-9a-z]+", "", t)


def safe_name(text: str, limit: int = 80) -> str:
    t = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", text or "").strip()
    return re.sub(r"\s+", " ", t)[:limit].strip() or "short"


def local_time(settings: Settings, when: datetime | None) -> str:
    """Publish time as Roeland's phone shows it (settings.phone_timezone), e.g. 'Sat 8 Nov 18:00'."""
    if when is None:
        return "no time picked"
    try:
        from zoneinfo import ZoneInfo
        when = when.astimezone(ZoneInfo(settings.phone_timezone))
        zone = settings.phone_timezone.split("/")[-1].replace("_", " ") + " time"
    except Exception:  # noqa: BLE001 - no tz database (Windows without tzdata): show UTC
        when, zone = when.astimezone(timezone.utc), "UTC"
    return f"{when:%a} {when.day} {when:%b %H:%M} ({zone})"


def file_stem(settings: Settings, short_id: int, title: str, when: datetime | None) -> str:
    """'11-08 1800 Why octopuses have three hearts (412)': sorts by publish time in the phone's
    file list; the id keeps two Shorts with the same title apart."""
    if when is not None:
        try:
            from zoneinfo import ZoneInfo
            when = when.astimezone(ZoneInfo(settings.phone_timezone))
        except Exception:  # noqa: BLE001
            pass
        head = f"{when:%m-%d %H%M} "
    else:
        head = ""
    return f"{head}{safe_name(title)} ({short_id})"


def note_text(settings: Settings, meta: dict, when: datetime | None) -> str:
    tags = ", ".join(meta.get("tags") or [])
    return (f"PUBLISH: {local_time(settings, when)}\n\n"
            f"TITLE (paste exactly, the pipeline finds the upload by it):\n{meta['title'][:100]}\n\n"
            f"DESCRIPTION:\n{meta.get('description') or ''}\n\n"
            f"TAGS:\n{tags}\n")


# ---------- Drive calls ----------

def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def folder(drv, name: str, parent: str | None = None) -> str:
    """Id of the folder `name` (in `parent`, else in My Drive), made when missing."""
    q = f"name = '{_q(name)}' and mimeType = '{FOLDER}' and trashed = false"
    q += f" and '{parent}' in parents" if parent else " and 'root' in parents"
    found = drv.files().list(q=q, fields="files(id)", pageSize=1, spaces="drive").execute().get("files") or []
    if found:
        return found[0]["id"]
    body = {"name": name, "mimeType": FOLDER, "parents": [parent] if parent else ["root"]}
    return drv.files().create(body=body, fields="id").execute()["id"]


def put_file(drv, parent: str, path: Path | None, name: str, mimetype: str, text: str | None = None) -> str:
    from googleapiclient.http import MediaFileUpload, MediaInMemoryUpload
    media = (MediaInMemoryUpload(text.encode("utf-8"), mimetype=mimetype) if text is not None
             else MediaFileUpload(str(path), mimetype=mimetype, chunksize=8 << 20, resumable=True))
    return drv.files().create(body={"name": name, "parents": [parent]}, media_body=media, fields="id").execute()["id"]


def trash(drv, file_ids: list[str]) -> list[str]:
    """Move files to the Drive trash (recoverable for 30 days). Returns the ids that are handled
    (trashed now, or already gone)."""
    from googleapiclient.errors import HttpError
    done = []
    for fid in file_ids:
        try:
            drv.files().update(fileId=fid, body={"trashed": True}).execute()
            done.append(fid)
        except HttpError as e:
            if getattr(getattr(e, "resp", None), "status", None) == 404:
                done.append(fid)  # deleted by hand already
            else:
                log.warning("Drive: could not trash %s: %s", fid, e)
    return done


# ---------- daily upload, phone channels ----------

def send_approved(conn: sqlite3.Connection, settings: Settings, channel: Channel, limit: int | None = None,
                  drv=None, now: datetime | None = None) -> list[int]:
    """Approved Shorts of a phone channel to Drive, each with its publish slot. Returns the Short ids
    sent. They become 'on_drive' and keep their slot (publish_at), so later slots skip it."""
    from .render import metadata
    from .upload import iso, slots_for

    rows = conn.execute("SELECT * FROM shorts WHERE channel=? AND status='approved' ORDER BY id",
                        (channel.id,)).fetchall()
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return []
    drv = drv or service(settings)
    top = folder(drv, settings.drive_folder)
    sub = folder(drv, safe_name(channel.name, 60), top)
    slots = slots_for(conn, settings, channel, [r["id"] for r in rows], now=now)
    sent = []
    for s, slot in zip(rows, slots):
        if not s["video_path"] or not Path(s["video_path"]).exists():
            log.warning("short %d: video file missing, not sent to Drive", s["id"])
            continue
        meta = metadata(conn, channel, s["id"], settings)
        stem = file_stem(settings, s["id"], meta["title"], slot)
        ids = [put_file(drv, sub, Path(s["video_path"]), stem + ".mp4", "video/mp4"),
               put_file(drv, sub, None, stem + ".txt", "text/plain", note_text(settings, meta, slot))]
        extra = {} if s["title"] else {"title": meta["title"]}  # matched against the upload's title later
        db.set_short(conn, s["id"], status="on_drive", publish_at=iso(slot), drive_files=json.dumps(ids), **extra)
        log.info("short %d sent to Drive for the phone: %s (publish %s)", s["id"], stem, local_time(settings, slot))
        sent.append(s["id"])
    return sent


# ---------- spotting the upload ----------

def match(conn: sqlite3.Connection, channel: Channel, items: dict[str, dict], now: datetime | None = None) -> dict[str, int]:
    """Videos on YouTube (videos.list items by id) that are a Short waiting on Drive: same title
    (loosely, see norm). Each match gets the YouTube id, its state and publish time, like an API
    upload. Returns {youtube id: short id}."""
    from .ytstats import parse_time, state_of

    now = now or datetime.now(timezone.utc)
    waiting = conn.execute("SELECT id, title FROM shorts WHERE channel=? AND status='on_drive' ORDER BY id",
                           (channel.id,)).fetchall()
    if not waiting:
        return {}
    known = {r[0] for r in conn.execute("SELECT youtube_id FROM shorts WHERE youtube_id IS NOT NULL")}
    out: dict[str, int] = {}
    for vid, it in items.items():
        if vid in known:
            continue
        title = norm(it.get("snippet", {}).get("title"))
        if not title:
            continue
        short = next((w for w in waiting if w["id"] not in out.values() and norm(w["title"])
                      and (norm(w["title"]) == title or title.startswith(norm(w["title"])))), None)
        if short is None:
            continue
        state = state_of(it, now)
        st = it.get("status", {})
        when = parse_time(st.get("publishAt")) or parse_time(it.get("snippet", {}).get("publishedAt"))
        status = {"scheduled": "scheduled", "live": "published"}.get(state, "uploaded")
        db.set_short(conn, short["id"], status=status, youtube_id=vid,
                     publish_at=when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None,
                     uploaded_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        log.info("%s: short %d was posted from the phone as https://youtu.be/%s (%s)", channel.id, short["id"], vid, state)
        out[vid] = short["id"]
    return out


def cleanup(conn: sqlite3.Connection, settings: Settings, drv=None) -> int:
    """Trash the Drive files of Shorts that are on YouTube now (or were rejected). Returns how many
    Shorts were cleaned up; files that fail stay listed and are tried again next time."""
    rows = conn.execute("""SELECT id, drive_files FROM shorts WHERE drive_files IS NOT NULL AND drive_files != '[]'
                            AND (youtube_id IS NOT NULL OR status = 'rejected')""").fetchall()
    if not rows:
        return 0
    if drv is None:
        if not connected(settings):
            return 0
        drv = service(settings)
    n = 0
    for r in rows:
        ids = json.loads(r["drive_files"] or "[]")
        left = [f for f in ids if f not in trash(drv, ids)]
        db.set_short(conn, r["id"], drive_files=json.dumps(left) if left else None)
        n += not left
    if n:
        log.info("Drive: removed the files of %d Short(s) that are posted now", n)
    return n


def check_all(settings: Settings) -> str:
    """For every phone channel with Shorts waiting on Drive: read its videos from YouTube (a few
    quota units), match, then clean Drive up. Never opens a browser."""
    from googleapiclient.discovery import build

    from . import upload
    from .config import all_channels
    from .ytstats import channel_videos

    conn = db.connect(settings.db_path)
    try:
        matched = 0
        for ch in all_channels(settings.config_dir):
            if not conn.execute("SELECT 1 FROM shorts WHERE channel=? AND status='on_drive' LIMIT 1", (ch.id,)).fetchone():
                continue
            if not upload.token_path(settings, ch.id).exists():
                continue
            try:
                yt = build("youtube", "v3", credentials=upload.credentials(settings, ch, interactive=False),
                           cache_discovery=False)
                _, items, _ = channel_videos(yt, [], part="snippet,status")
                matched += len(match(conn, ch, items))
            except Exception as e:  # noqa: BLE001 - one channel must not stop the others
                log.warning("%s: phone upload check failed: %s", ch.id, e)
        cleaned = cleanup(conn, settings)
        return f"{matched} posted from the phone, Drive files removed for {cleaned}"
    finally:
        conn.close()


def start_checker(settings: Settings) -> None:
    """Dashboard: check_all every CHECK_EVERY seconds in a background thread."""
    import threading
    import time

    def loop():
        while True:
            time.sleep(CHECK_EVERY)
            try:
                result = check_all(settings)
                log.debug("phone check: %s", result)
            except Exception:  # noqa: BLE001
                log.exception("phone check")

    threading.Thread(target=loop, name="phone-check", daemon=True).start()
