"""Running on the Linux server (see deploy/README.md): secrets in the environment, database backups."""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

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
