"""Daily auto-run: for every channel with auto_run = true, make that day's Shorts and schedule the
approved ones, then fetch stats for every channel. Nothing goes public without approval in review. Windows Task Scheduler runs
`python -m shorts daily` once a day; the dashboard's Schedule page turns that task on and off."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime

from .config import Settings, list_channels, load_channel

log = logging.getLogger(__name__)

TASK = "shorts-daily"
BACKLOG_DAYS = 3  # don't make more while this many days of Shorts still wait for review


def run(conn: sqlite3.Connection, settings: Settings) -> dict[str, str]:
    from .pipeline import make_many
    from .upload import fetch_stats_all, upload_approved

    done = {}
    chans = [load_channel(cid, settings.config_dir) for cid in list_channels(settings.config_dir)]
    chans = [ch for ch in chans if ch.auto_run]
    waiting = {ch.id: conn.execute("SELECT COUNT(*) FROM shorts WHERE channel=? AND status='rendered'",
                                   (ch.id,)).fetchone()[0] for ch in chans}
    orders = []
    for ch in chans:
        if waiting[ch.id] < BACKLOG_DAYS * ch.uploads_per_day:
            orders.append((ch, ch.uploads_per_day, None))
        else:
            log.info("%s: %d Shorts still wait for review; not making more today", ch.id, waiting[ch.id])
    # One run for all channels: Claude writes the next channel while the previous one renders.
    ready = make_many(conn, settings, orders)
    for ch in chans:
        made = ready.get(ch.id, waiting[ch.id]) - waiting[ch.id]
        try:
            uploaded = len(upload_approved(conn, settings, ch, interactive=False))
        except Exception as e:  # noqa: BLE001 - one channel's login problem must not stop the others
            log.warning("%s: scheduling failed: %s", ch.id, e)
            uploaded = 0
        done[ch.id] = f"made {max(made, 0)}, scheduled {uploaded}"
        log.info("%s: %s", ch.id, done[ch.id])
    fetch_stats_all(conn, settings)
    return done


def _state_path(settings: Settings):
    return settings.data_dir / "daily.json"


def status(settings: Settings) -> dict:
    out = {"channels": [], "task": None, "time": None, "note": ""}
    for cid in list_channels(settings.config_dir):
        try:
            if load_channel(cid, settings.config_dir).auto_run:
                out["channels"].append(cid)
        except Exception:  # noqa: BLE001
            pass
    try:
        out["time"] = json.loads(_state_path(settings).read_text(encoding="utf-8")).get("time")
    except (OSError, ValueError):
        pass
    if os.name != "nt":
        out["note"] = "Windows Task Scheduler only"
        return out
    q = subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True, text=True)
    out["task"] = q.returncode == 0
    return out


def set_task(settings: Settings, on: bool, at: str = "09:00") -> str:
    """Create (or replace) / delete the Windows task. Returns a message for the page."""
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("time must be HH:MM")
    if os.name != "nt":
        return "The daily task uses Windows Task Scheduler; on this computer run 'python -m shorts daily' yourself."
    if on:
        cmd = (f'"{sys.executable}" -m shorts --data-dir "{settings.data_dir}" '
               f'--config-dir "{settings.config_dir}" daily')
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK, "/TR", cmd, "/SC", "DAILY", "/ST", at, "/F"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip())
        _state_path(settings).write_text(json.dumps({"time": at, "since": datetime.now().isoformat()}), encoding="utf-8")
        return f"Daily task on at {at}."
    subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True, text=True)
    return "Daily task off."
