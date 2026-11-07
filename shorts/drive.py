"""Upload via phone (research/app-music-workflow.md): for channels with upload_via = "phone" the
daily upload doesn't post to YouTube. It puts each approved Short (rendered without music) in
Google Drive, in "<drive_folder>/" (one channel at a time: the app shows no folders, Home says
which channel is on the phone), with a .txt next to it holding the title,
description, tags and publish time. The Autosync app on Roeland's phone keeps that folder in sync
with a phone folder, so the Shorts show up in the gallery; he posts them in the YouTube app with a
trending sound ("Add sound"). As title he types only the first words on screen, which the file
on screen (key_words, always exactly 3) and posts it as Private without picking a time. Two Shorts
of a channel that start with the same words are never on Drive at the same time.

The stats sync (and check_all every 30 minutes from the dashboard) looks for new videos on the
channel whose title starts with a waiting Short's key words (or is its full title). A match gets
the YouTube id like an API upload (so stats and learning work); with the "manage" YouTube login
(Connect YouTube again, see upload.scopes_for) the server then sets the real title, description,
tags and, for a private video, the publish time. Its Drive file goes to the Drive trash, which
Autosync mirrors by deleting it from the phone.

The Drive login uses scope drive.file: the app only sees the files and folders it made itself."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
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


KEY_WORDS = 3
SAME_LENGTH = 1.0  # seconds: YouTube rounds durations to whole seconds


def words_on_screen(conn: sqlite3.Connection, short_id: int) -> list[str]:
    """Words of the Short's first caption block (what is on screen first), else of its title."""
    row = conn.execute("SELECT text FROM blocks WHERE short_id=? ORDER BY idx LIMIT 1", (short_id,)).fetchone()
    text = row[0] if row and row[0] else (conn.execute("SELECT title FROM shorts WHERE id=?", (short_id,)).fetchone() or [""])[0]
    return re.findall(r"[0-9A-Za-zÀ-ɏ']+", text or "")


def key_words(conn: sqlite3.Connection, short_id: int) -> str:
    """The first KEY_WORDS words on screen: what Roeland types as title (always exactly that many)."""
    return " ".join((words_on_screen(conn, short_id) or [str(short_id)])[:KEY_WORDS])


def clashes(key: str, others: list[str]) -> bool:
    """Two waiting Shorts of one channel whose typed words could be confused."""
    k = norm(key)
    return any(k.startswith(norm(o)) or norm(o).startswith(k) for o in others if norm(o))


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


def file_stem(settings: Settings, key: str, when: datetime | None) -> str:
    """'Octopuses have three - za 08-11 18.00': starts with the words to type as title, then the
    publish time on the phone's clock."""
    if when is None:
        return safe_name(key)
    try:
        from zoneinfo import ZoneInfo
        when = when.astimezone(ZoneInfo(settings.phone_timezone))
    except Exception:  # noqa: BLE001
        pass
    return f"{safe_name(key)} - {when:%d-%m %H.%M}"


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

def on_phone(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The channel whose Shorts are on Drive now ({channel, n}), or None. Only one channel at a time:
    the YouTube app can't see folders, so everything in the folder is for one channel."""
    return conn.execute("""SELECT channel, COUNT(*) AS n FROM shorts WHERE status='on_drive'
                           GROUP BY channel ORDER BY MIN(id) LIMIT 1""").fetchone()


def send_next(conn: sqlite3.Connection, settings: Settings, drv=None, now: datetime | None = None) -> list[int]:
    """The phone is empty: the phone channel with the oldest approved Short goes next."""
    from .config import all_channels

    if on_phone(conn):
        return []
    phone = {c.id: c for c in all_channels(settings.config_dir) if is_phone(c)}
    if not phone:
        return []
    row = conn.execute(f"""SELECT channel FROM shorts WHERE status='approved' AND channel IN ({','.join('?' * len(phone))})
                           ORDER BY id LIMIT 1""", list(phone)).fetchone()
    if not row:
        return []
    return send_approved(conn, settings, phone[row[0]], drv=drv, now=now)


def send_approved(conn: sqlite3.Connection, settings: Settings, channel: Channel, limit: int | None = None,
                  drv=None, now: datetime | None = None) -> list[int]:
    """Approved Shorts of a phone channel to Drive, each with its publish slot. Returns the Short ids
    sent. They become 'on_drive' and keep their slot (publish_at), so later slots skip it."""
    from .render import metadata
    from .upload import iso, slots_for

    other = on_phone(conn)
    if other and other["channel"] != channel.id:
        log.info("'%s' waits: the phone still has %d Short(s) of '%s' to post", channel.id, other["n"], other["channel"])
        return []
    rows = conn.execute("SELECT * FROM shorts WHERE channel=? AND status='approved' ORDER BY id",
                        (channel.id,)).fetchall()
    # Never two waiting Shorts that start with the same words: the later one waits for the next run.
    taken = [r[0] for r in conn.execute(
        "SELECT phone_key FROM shorts WHERE channel=? AND status='on_drive' AND phone_key IS NOT NULL", (channel.id,))]
    keys, ok = {}, []
    for r in rows:
        key = key_words(conn, r["id"])
        if clashes(key, taken):
            log.info("short %d starts like a Short still waiting on Drive ('%s'): it goes after that one is posted",
                     r["id"], key)
            continue
        taken.append(key)
        keys[r["id"]] = key
        ok.append(r)
    rows = ok
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return []
    drv = drv or service(settings)
    sub = folder(drv, settings.drive_folder)  # one flat folder: only one channel's Shorts at a time
    slots = slots_for(conn, settings, channel, [r["id"] for r in rows], now=now)
    sent = []
    for s, slot in zip(rows, slots):
        if not s["video_path"] or not Path(s["video_path"]).exists():
            log.warning("short %d: video file missing, not sent to Drive", s["id"])
            continue
        meta = metadata(conn, channel, s["id"], settings)
        key = keys[s["id"]]
        ids = [put_file(drv, sub, Path(s["video_path"]), file_stem(settings, key, slot) + ".mp4", "video/mp4")]
        extra = {} if s["title"] else {"title": meta["title"]}
        db.set_short(conn, s["id"], status="on_drive", publish_at=iso(slot), drive_files=json.dumps(ids),
                     phone_key=key, drive_at=iso(now or datetime.now(timezone.utc)), **extra)
        log.info("short %d sent to Drive for the phone: type '%s' as title (publish %s)", s["id"], key,
                 local_time(settings, slot))
        sent.append(s["id"])
    return sent


# ---------- spotting the upload ----------

def match(conn: sqlite3.Connection, channel: Channel, items: dict[str, dict], now: datetime | None = None) -> dict[str, int]:
    """Videos on YouTube (videos.list items by id) that are a Short waiting on Drive: same title
    (loosely, see norm). Each match gets the YouTube id, its state and publish time, like an API
    upload. Returns {youtube id: short id}."""
    from .ytstats import duration_s, parse_time, state_of

    now = now or datetime.now(timezone.utc)
    waiting = conn.execute("""SELECT id, title, phone_key, duration, drive_at FROM shorts
                              WHERE channel=? AND status='on_drive' ORDER BY id""",
                           (channel.id,)).fetchall()
    if not waiting:
        return {}
    known = {r[0] for r in conn.execute("SELECT youtube_id FROM shorts WHERE youtube_id IS NOT NULL")}
    out: dict[str, int] = {}
    for vid, it in items.items():
        if vid in known:
            continue
        title = norm(it.get("snippet", {}).get("title"))

        def hit(w) -> bool:
            key, full = norm(w["phone_key"]), norm(w["title"])
            return bool(key and title.startswith(key)) or bool(full and title.startswith(full))

        free = [w for w in waiting if w["id"] not in out.values()]
        hits = [w for w in free if hit(w)]
        if not hits:
            # typo in the words: the one waiting Short of the same length (to the second), posted after
            # it went to Drive
            secs = duration_s(it.get("contentDetails", {}).get("duration"))
            posted = parse_time(it.get("snippet", {}).get("publishedAt"))
            hits = [w for w in free if secs is not None and w["duration"] and abs(w["duration"] - secs) <= SAME_LENGTH
                    and (not posted or not w["drive_at"] or posted >= parse_time(w["drive_at"]))]
            if len(hits) != 1:
                continue
        short = max(hits, key=lambda w: len(norm(w["phone_key"])))
        if short is None:
            continue
        state = state_of(it, now)
        st = it.get("status", {})
        when = parse_time(st.get("publishAt")) or parse_time(it.get("snippet", {}).get("publishedAt"))
        status = {"scheduled": "scheduled", "live": "published"}.get(state, "uploaded")
        fields = {"status": status, "youtube_id": vid, "uploaded_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "filled": 0}
        if state in ("scheduled", "live") and when:
            fields["publish_at"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")  # his own time wins; else keep the slot
        db.set_short(conn, short["id"], **fields)
        log.info("%s: short %d was posted from the phone as https://youtu.be/%s (%s)", channel.id, short["id"], vid, state)
        out[vid] = short["id"]
    return out


def fill(conn: sqlite3.Connection, settings: Settings, channel: Channel, yt, matched: dict[str, int],
         items: dict[str, dict], now: datetime | None = None) -> int:
    """After a match: the real title, description, tags and category, like an API upload. A video
    he left private without a time gets the Short's slot as publish time (or goes public right away
    when the slot is past); one he scheduled or published himself keeps that. Needs the "manage"
    YouTube login; without it nothing changes here (Home asks to connect again). Returns how many
    videos were updated."""
    from .render import metadata
    from .upload import body_for, can_manage, iso
    from .ytstats import parse_time, state_of

    if not matched:
        return 0
    if not can_manage(settings, channel):
        log.warning("%s: %d phone upload(s) found, but the YouTube login may not change videos: press Connect again "
                    "under YouTube on the channel's Settings page; title etc. are set on the next check",
                    channel.id, len(matched))
        return 0
    now = now or datetime.now(timezone.utc)
    done = 0
    for vid, sid in matched.items():
        it = items.get(vid) or {}
        state = state_of(it, now)
        row = conn.execute("SELECT publish_at FROM shorts WHERE id=?", (sid,)).fetchone()
        slot = parse_time(row["publish_at"]) if row else None
        body = body_for(metadata(conn, channel, sid, settings), None)
        body["id"] = vid
        parts = "snippet"
        if state == "private":
            parts = "snippet,status"
            if slot and slot > now + timedelta(minutes=15):
                body["status"]["publishAt"] = iso(slot)
            else:
                body["status"]["privacyStatus"] = "public"
                slot = now
        else:
            body.pop("status")
        try:
            yt.videos().update(part=parts, body=body).execute()
        except Exception as e:  # noqa: BLE001 - try again next check
            log.warning("%s: could not set title etc. on %s: %s", channel.id, vid, e)
            conn.execute("UPDATE shorts SET filled=0 WHERE id=?", (sid,))
            conn.commit()
            continue
        fields = {"filled": 1}
        if state == "private":
            fields.update(status="scheduled" if slot > now else "published", publish_at=iso(slot))
        db.set_short(conn, sid, **fields)
        log.info("%s: short %d (%s): title, description and tags set%s", channel.id, sid, vid,
                 f", publishes {local_time(settings, slot)}" if state == "private" else "")
        done += 1
    return done


def unfilled(conn: sqlite3.Connection, channel: Channel) -> dict[str, int]:
    """Posted from the phone, matched, but title etc. not set yet (no manage login then, or it failed)."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT youtube_id, id FROM shorts WHERE channel=? AND filled=0 AND youtube_id IS NOT NULL", (channel.id,))}


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
            if not conn.execute("""SELECT 1 FROM shorts WHERE channel=? AND (status='on_drive' OR filled=0)
                                    LIMIT 1""", (ch.id,)).fetchone():
                continue
            if not upload.token_path(settings, ch.id).exists():
                continue
            try:
                yt = build("youtube", "v3", credentials=upload.credentials(settings, ch, interactive=False),
                           cache_discovery=False)
                todo = unfilled(conn, ch)
                _, items, _ = channel_videos(yt, list(todo), part="snippet,status,contentDetails")
                found = match(conn, ch, items)
                matched += len(found)
                fill(conn, settings, ch, yt, {**todo, **found}, items)
            except Exception as e:  # noqa: BLE001 - one channel must not stop the others
                log.warning("%s: phone upload check failed: %s", ch.id, e)
        cleaned = cleanup(conn, settings)
        sent = send_next(conn, settings) if connected(settings) else []
        return (f"{matched} posted from the phone, Drive files removed for {cleaned}"
                + (f", {len(sent)} new on Drive" if sent else ""))
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
