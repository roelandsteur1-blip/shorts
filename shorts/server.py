"""Running on a Linux server (see deploy/README.md): database backups and moving the database
from the laptop, whose stored paths (D:\\shorts-footage\\...) don't exist on the server."""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

# Every column that stores a file path.
PATH_COLUMNS = [("assets", "path"), ("scenes", "keyframe"), ("shorts", "video_path"),
                ("jobs", "log_path"), ("tracks", "path")]
KEEP_BACKUPS = 7
ENV_FILE = "server.env"


def load_env(settings: Settings) -> None:
    """KEY=VALUE lines from secrets/server.env into the environment (not overriding what is set).
    On the server it holds CLAUDE_CODE_OAUTH_TOKEN, so the dashboard, its jobs and the cron task
    all reach Claude without a browser login."""
    path = settings.secrets_dir / ENV_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _norm(p: str) -> str:
    return p.replace("\\", "/").rstrip("/")


def rebase_paths(conn: sqlite3.Connection, pairs: list[tuple[str, str]]) -> dict[str, int]:
    """Rewrite stored paths that start with an old folder (any slash style, any letter case) to
    the new folder, with forward slashes. pairs: (old, new); the longest old folder wins.
    Returns rows changed per table.column."""
    pairs = sorted(((_norm(o), _norm(n)) for o, n in pairs), key=lambda p: -len(p[0]))
    changed: dict[str, int] = {}
    for table, col in PATH_COLUMNS:
        rows = conn.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
        n = 0
        for row_id, value in rows:
            v = _norm(value)
            for old, new in pairs:
                if v.lower() == old.lower() or v.lower().startswith(old.lower() + "/"):
                    conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (new + v[len(old):], row_id))
                    n += 1
                    break
        changed[f"{table}.{col}"] = n
    conn.commit()
    return changed


def backup(conn: sqlite3.Connection, settings: Settings, dest: Path | None = None) -> Path:
    """A consistent copy of the database (safe while jobs write to it). Without dest it goes to
    data_dir/backups/shorts-<date>.db and only the newest KEEP_BACKUPS are kept."""
    if dest is None:
        folder = settings.data_dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"shorts-{datetime.now():%Y%m%d-%H%M}.db"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()
    if dest.parent == settings.data_dir / "backups":
        for old in sorted(dest.parent.glob("shorts-*.db"))[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
    log.info("database backed up to %s", dest)
    return dest
