"""Clip quality benchmark: which ways of picking, timing, cropping and judging clips work best.

A research tool, not part of the daily pipeline (plan: plan/clip-quality-research-plan.md in the
project files). It runs on the server in its own venv (deploy/bench.sh) and works on a copy of the
database, so the daily run is never touched. Everything lands in data_dir/bench/:

    testset   pick ~60 real b-roll blocks (spread over channels) and a library of scenes around them
    frames    3 frames per library scene (15%, 50%, 85% of the shot)
    embed     every image-text model embeds those frames (timed: CPU seconds per image)
    rank      every search method ranks the library for every block (top 20 each)
    asr       what the narrator of the original film says during each scene (Whisper)
    judge     Claude labels the pooled top candidates of all methods: on topic 0-2, interest 1-5
    clips     for ~40 good clips: which seconds and which 9:16 crop each method picks
    clip-judge  Claude rates those seconds and crops
    interest  cheap "worth watching" scores vs Claude's interest ratings
    chains    step 4: a sample of moments rendered three ways (today, winner, cheap) for a blind
              review on the dashboard (/bench/review)
    report    one table per job, with CPU cost (report.md)

Each stage keeps its results in files and skips work already done, so a stopped run (or a Claude
usage limit) continues where it was. `python -m shorts.bench status` says how far it is."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from ..config import Settings

SEED = 7
FOLDER = "bench"   # "bench-smoke" for a quick trial run that leaves the real results alone


def root(settings: Settings) -> Path:
    d = settings.data_dir / FOLDER
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(path: Path, data) -> None:
    """Written to a temp file first: a run stopped mid-write never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def snapshot(settings: Settings) -> sqlite3.Connection:
    """The bench's own copy of the database (made once): the live one keeps changing and is
    written to by the daily run."""
    path = root(settings) / "shorts.db"
    if not path.exists():
        src = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(path)
        src.backup(dst)
        src.close()
        dst.close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def status_line(settings: Settings, stage: str, done: int, total: int, note: str = "") -> None:
    """What the run is doing now, for `status` and the dashboard's Bench page. One line per kind
    of work (the big models embed while Claude judges), newest first."""
    path = root(settings) / "progress.json"
    lines = load(path, {})
    if not isinstance(lines, dict) or "stage" in lines:
        lines = {}
    lines[stage.split()[0].split(":")[0]] = {"stage": stage, "done": done, "total": total, "note": note,
                                             "at": time.time()}
    save(path, dict(sorted(lines.items(), key=lambda kv: -kv[1]["at"])))
