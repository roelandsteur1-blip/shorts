"""SQLite storage. One database for all channels; every short records its channel."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,              -- 'nasa', 'svs', 'esa', ...
    source_id     TEXT NOT NULL,              -- e.g. NASA nasa_id
    title         TEXT,
    description   TEXT,
    keywords      TEXT,                       -- comma separated
    credit        TEXT,                       -- center / photographer / secondary_creator
    license       TEXT NOT NULL,              -- 'public-domain', 'CC BY 4.0', ...
    attribution   TEXT,                       -- text that must appear in the description, if any
    needs_check   INTEGER NOT NULL DEFAULT 0, -- 1 = credit not plainly NASA: check rights
    source_url    TEXT,                       -- human page for the item
    file_url      TEXT,                       -- the file we downloaded
    path          TEXT,                       -- local, audio stripped
    duration      REAL,
    width         INTEGER,
    height        INTEGER,
    date_created  TEXT,
    status        TEXT NOT NULL DEFAULT 'new',-- new, downloaded, scened, failed, duplicate
    fingerprint   BLOB,                       -- frame hashes, to spot re-published footage
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS scenes (
    id            INTEGER PRIMARY KEY,
    asset_id      INTEGER NOT NULL REFERENCES assets(id),
    start         REAL NOT NULL,
    end           REAL NOT NULL,
    keyframe      TEXT,
    motion        REAL,                       -- mean frame difference, 0..1
    contrast      REAL,                       -- luminance std, 0..1
    brightness    REAL,
    text_like     REAL,                       -- share of edge pixels typical for title cards
    crop_x        REAL,                       -- 0..1 centre of the 9:16 crop
    rejected      INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT,
    embedding     BLOB                        -- float32 CLIP image embedding (normalised)
);
CREATE INDEX IF NOT EXISTS scenes_asset ON scenes(asset_id);

CREATE TABLE IF NOT EXISTS shorts (
    id             INTEGER PRIMARY KEY,
    channel        TEXT NOT NULL,
    format_id      TEXT NOT NULL,
    hero_scene_id  INTEGER REFERENCES scenes(id),
    topic          TEXT,
    hook           TEXT,
    title          TEXT,
    description    TEXT,
    tags           TEXT,
    status         TEXT NOT NULL DEFAULT 'planned',
        -- planned, scripted, checked, rendered, approved, rejected, scheduled, uploaded (private,
        -- no publish time), failed
    factcheck_ok   INTEGER,                    -- 1 all facts supported, 0 at least one flagged
    video_path     TEXT,
    duration       REAL,
    music          TEXT,
    synthetic      INTEGER NOT NULL DEFAULT 0, -- containsSyntheticMedia
    reject_reason  TEXT,
    youtube_id     TEXT,
    publish_at     TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS shorts_status ON shorts(channel, status);

CREATE TABLE IF NOT EXISTS blocks (
    id        INTEGER PRIMARY KEY,
    short_id  INTEGER NOT NULL REFERENCES shorts(id),
    idx       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    seconds   REAL NOT NULL,
    visual    TEXT,                            -- what the shot should show (for search)
    scene_id  INTEGER REFERENCES scenes(id)
);

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY,
    short_id    INTEGER NOT NULL REFERENCES shorts(id),
    claim       TEXT NOT NULL,
    source_url  TEXT,
    source_quote TEXT,
    verdict     TEXT,                          -- supported, unsupported, unclear
    note        TEXT
);

CREATE TABLE IF NOT EXISTS stats (
    short_id       INTEGER NOT NULL REFERENCES shorts(id),
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now')),
    views          INTEGER,
    engaged_views  INTEGER,
    avg_view_pct   REAL,
    avg_view_dur   REAL,
    likes          INTEGER,
    shares         INTEGER,
    subs_gained    INTEGER
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
    if "fingerprint" not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN fingerprint BLOB")
        conn.commit()


def set_short(conn: sqlite3.Connection, short_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE shorts SET {cols}, updated_at = datetime('now') WHERE id = ?",
        (*fields.values(), short_id),
    )
    conn.commit()


def used_scene_ids(conn: sqlite3.Connection, channel: str) -> set[int]:
    """Scenes already used by a non-rejected short on this channel."""
    rows = conn.execute(
        """
        SELECT s.hero_scene_id AS sid FROM shorts s
         WHERE s.channel = ? AND s.status != 'rejected' AND s.hero_scene_id IS NOT NULL
        UNION
        SELECT b.scene_id FROM blocks b JOIN shorts s ON s.id = b.short_id
         WHERE s.channel = ? AND s.status != 'rejected' AND b.scene_id IS NOT NULL
        """,
        (channel, channel),
    ).fetchall()
    return {r[0] for r in rows}


def used_hero_assets(conn: sqlite3.Connection, channel: str) -> set[int]:
    rows = conn.execute(
        """SELECT sc.asset_id FROM shorts s JOIN scenes sc ON sc.id = s.hero_scene_id
            WHERE s.channel = ? AND s.status != 'rejected'""",
        (channel,),
    ).fetchall()
    return {r[0] for r in rows}
