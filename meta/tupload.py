"""Upload the transcript-first channels' Shorts (Roeland 2026-11-14 "will the channels upload their
Shorts automatically"). Per channel, ~/meta-out/tclips/channels/<id>/upload.json holds the switch:

    {"on": false, "mode": "auto", "per_day": 1, "skip": [], "approved": []}

mode "auto": the best Short not yet up goes next; mode "approve": only Shorts in "approved".
Uploads stay one day ahead: a channel gets a new upload only while it has fewer than per_day
Shorts waiting to publish, each a private video with a publish time (16:00 UTC, then 20:00).
Off by default; a channel needs a confirmed YouTube login (Connect YouTube) before anything goes up.
The dashboard runs `python -m meta.tupload` every hour.

    python -m meta.tupload              # every channel that is on
    python -m meta.tupload --channel X  # one channel
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path.home() / "meta-out" / "tclips"
FORMAT_ID = "tclips"
TIMES = ("16:00", "20:00")
DEFAULT = {"on": False, "mode": "auto", "per_day": 1, "skip": [], "approved": []}


def cfg_path(base: Path, cid: str) -> Path:
    return base / "channels" / cid / "upload.json"


def load_cfg(base: Path, cid: str) -> dict:
    try:
        data = json.loads(cfg_path(base, cid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    cfg = {**DEFAULT, **{k: v for k, v in data.items() if k in DEFAULT}}
    cfg["per_day"] = max(1, min(int(cfg["per_day"] or 1), len(TIMES)))
    cfg["mode"] = cfg["mode"] if cfg["mode"] in ("auto", "approve") else "auto"
    return cfg


def save_cfg(base: Path, cid: str, cfg: dict) -> None:
    path = cfg_path(base, cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: cfg.get(k, v) for k, v in DEFAULT.items()}, indent=1), encoding="utf-8")


def made(base: Path, cid: str) -> list[dict]:
    try:
        return json.loads((base / "channels" / cid / "made.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def queue(base: Path, cid: str, done_paths: set[str], cfg: dict | None = None) -> list[dict]:
    """The channel's Shorts that may still go up, in upload order (best score first, then oldest),
    each with its "path"."""
    cfg = cfg or load_cfg(base, cid)
    skip, ok = set(cfg["skip"]), set(cfg["approved"])
    out = []
    for i, m in enumerate(made(base, cid)):
        name = m.get("short")
        path = base / "channels" / cid / f"{name}.mp4"
        if not name or name in skip or str(path) in done_paths or not path.exists() or m.get("failed"):
            continue
        if cfg["mode"] == "approve" and name not in ok:
            continue
        out.append({**m, "path": str(path), "secs": (m.get("lengths") or {}).get(path.name), "_i": i})
    out.sort(key=lambda m: (-(m.get("score") or 0), m["_i"]))
    return out


def done_paths(conn, cid: str) -> set[str]:
    """Every Short that went up once, whatever happened to it after. Only a failed upload may go again.
    (Before 2026-11-18 this missed 'published', which the Stats sync sets once a Short is live, so each
    published Short was uploaded again the next day: Roeland "0 views across the board".)"""
    return {r[0] for r in conn.execute(
        "SELECT video_path FROM shorts WHERE channel=? AND format_id=? AND status != 'failed' AND video_path IS NOT NULL",
        (cid, FORMAT_ID))}


def waiting(conn, cid: str, now: datetime) -> list[str]:
    from shorts.upload import iso
    return [r[0] for r in conn.execute(
        "SELECT publish_at FROM shorts WHERE channel=? AND format_id=? AND status='scheduled' AND publish_at > ?",
        (cid, FORMAT_ID, iso(now)))]


def next_slot(conn, cid: str, per_day: int, now: datetime) -> datetime:
    """The first free publish time (TIMES[:per_day] each day) after the last one planned, at least
    an hour from now."""
    from shorts.upload import iso
    taken = {r[0] for r in conn.execute("SELECT publish_at FROM shorts WHERE channel=? AND publish_at IS NOT NULL", (cid,))}
    last = max(taken, default=None)
    after = max(now + timedelta(hours=1), datetime.fromisoformat(last.replace("Z", "+00:00")) if last else now)
    day = after.date()
    while True:
        for t in TIMES[:per_day]:
            slot = datetime.combine(day, datetime.strptime(t, "%H:%M").time(), tzinfo=timezone.utc)
            if slot > after and iso(slot) not in taken:
                return slot
        day += timedelta(days=1)


def upload_channel(cid: str, conn=None, settings=None, now: datetime | None = None, base: Path = OUT) -> list[str]:
    from googleapiclient.http import MediaFileUpload

    from shorts import db as sdb
    from shorts import upload as up
    from shorts.config import load_channel

    from .tclips import log, upload_meta
    now = now or datetime.now(timezone.utc)
    cfg = load_cfg(base, cid)
    if not cfg["on"]:
        return []
    ch = load_channel(cid, settings.config_dir)
    if not ch.youtube_channel_id or not up.token_path(settings, cid).exists():
        log(f"[upload] {cid}: not connected to YouTube (Connect YouTube on the Channels page), nothing uploaded")
        return []
    room = cfg["per_day"] - len(waiting(conn, cid, now))
    todo = queue(base, cid, done_paths(conn, cid), cfg)
    log(f"[upload] {cid}: {len(todo)} Shorts ready, {cfg['per_day']} per day, room for {max(room, 0)} now")
    if room <= 0 or not todo:
        return []
    until = up.blocked_until(settings, ch, now)
    if until or settings.upload_quota - up.uploads_today(conn, now) <= 0:
        log(f"[upload] {cid}: YouTube's daily limit reached, trying again later")
        return []
    yt = up.youtube(settings, ch, interactive=False)
    up.check_channel(yt, ch)
    out = []
    for m in todo[:room]:
        meta = upload_meta(m, ch, settings)
        slot = next_slot(conn, cid, cfg["per_day"], now)
        cur = conn.execute(
            """INSERT INTO shorts (channel, format_id, topic, hook, title, description, tags, status, video_path,
                                   duration) VALUES (?, ?, ?, ?, ?, ?, ?, 'rendered', ?, ?)""",
            (cid, FORMAT_ID, m.get("why"), m.get("hook"), meta["title"], meta["description"],
             json.dumps(meta["tags"]), m["path"], m.get("secs")))
        conn.commit()
        sid = cur.lastrowid
        try:
            req = yt.videos().insert(part="snippet,status", body=up.body_for(meta, slot),
                                     media_body=MediaFileUpload(m["path"], mimetype="video/mp4", chunksize=8 << 20,
                                                                resumable=True))
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
        except Exception as e:  # noqa: BLE001
            sdb.set_short(conn, sid, status="failed", error=str(e)[:500])
            if up._limit_reason(e) == "uploadLimitExceeded":
                up._blocked_path(settings, ch).write_text(up.iso(now), encoding="utf-8")
            log(f"[upload] {cid}: {m['short']} failed: {e}")
            break
        sdb.set_short(conn, sid, status="scheduled", youtube_id=resp["id"], publish_at=up.iso(slot),
                      uploaded_at=up.iso(datetime.now(timezone.utc)))
        out.append(resp["id"])
        log(f"[upload] {cid}: {m['short']} -> https://youtu.be/{resp['id']} publishes {up.iso(slot)}: {meta['title']}")
    return out


def connected(settings, cid: str) -> bool:
    """A confirmed YouTube login: the channel file has youtube_channel_id and the token exists."""
    from shorts import upload as up
    from shorts.config import load_channel
    try:
        ch = load_channel(cid, settings.config_dir)
    except Exception:  # noqa: BLE001 - no channel file yet: never connected
        return False
    return bool(ch.youtube_channel_id) and up.token_path(settings, cid).exists()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel")
    ap.add_argument("--enable", action="store_true",
                    help="turn the switch on (1 a day, best first) for every connected channel, then upload")
    a = ap.parse_args(argv)
    from .common import open_library
    from .tclips import load_tchannels
    settings, conn, _llm = open_library()
    ids = [a.channel] if a.channel else [c["id"] for c in load_tchannels()]
    if a.enable:
        for cid in ids:
            if connected(settings, cid):
                cfg = load_cfg(OUT, cid)
                if not cfg["on"]:
                    cfg.update(on=True, mode="auto", per_day=1)
                    save_cfg(OUT, cid, cfg)
                print(f"[upload] {cid}: connected, upload on ({cfg['per_day']} a day, {cfg['mode']}), "
                      f"{len(queue(OUT, cid, done_paths(conn, cid), cfg))} Shorts left to upload", flush=True)
            else:
                print(f"[upload] {cid}: not connected to YouTube, left off", flush=True)
    for cid in ids:
        try:
            upload_channel(cid, conn, settings)
        except Exception as e:  # noqa: BLE001 - one channel failing must not stop the rest
            print(f"[upload] {cid}: failed: {e}", flush=True)


if __name__ == "__main__":
    main()
