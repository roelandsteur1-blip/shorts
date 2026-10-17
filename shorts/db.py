"""SQLite storage. One database for all channels; every short records its channel."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,              -- shorts/sources module: 'nasa', 'internet_archive', ...
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
    status        TEXT NOT NULL DEFAULT 'new',-- new, downloaded, scened, failed, duplicate, rejected (rights), cleaned
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

-- Footage categories per scene (shorts/categorize.py). confidence 0 + method 'manual' means
-- "not this category" (a correction by hand that a re-sort keeps).
CREATE TABLE IF NOT EXISTS scene_categories (
    scene_id    INTEGER NOT NULL REFERENCES scenes(id),
    category    TEXT NOT NULL,
    confidence  REAL NOT NULL,
    method      TEXT NOT NULL,                 -- source, metadata, clip, metadata+clip, claude, manual
    PRIMARY KEY (scene_id, category)
);
CREATE INDEX IF NOT EXISTS scene_categories_cat ON scene_categories(category);

-- Rollup for quick counts: usable scenes per video and category.
CREATE TABLE IF NOT EXISTS asset_categories (
    asset_id    INTEGER NOT NULL,
    category    TEXT NOT NULL,
    scenes      INTEGER NOT NULL,
    PRIMARY KEY (asset_id, category)
);

-- How long each step took (shorts/timing.py), for the Stats page and 'python -m shorts bench'.
CREATE TABLE IF NOT EXISTS timings (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    stage     TEXT NOT NULL,                   -- download, fingerprint, scenes, embed, script, factcheck, render, ...
    seconds   REAL NOT NULL,
    items     INTEGER NOT NULL DEFAULT 1,
    info      TEXT                             -- JSON: video length, encoder, Claude tokens, ...
);
CREATE INDEX IF NOT EXISTS timings_stage ON timings(stage, at);

-- Background jobs started from the dashboard: each runs `python -m shorts --channel <id> <command>`.
CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY,
    channel        TEXT,
    command        TEXT NOT NULL,              -- JSON list of CLI arguments after --channel
    label          TEXT,
    heavy          INTEGER NOT NULL DEFAULT 1, -- 1 = uses GPU/CPU/Claude: one at a time
    status         TEXT NOT NULL DEFAULT 'queued',
        -- queued, running, done, failed, stopped, interrupted (laptop restarted mid-job)
    pid            INTEGER,
    exit_code      INTEGER,
    progress       TEXT,                       -- e.g. 'render'
    progress_done  INTEGER,
    progress_total INTEGER,
    log_path       TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    started_at     TEXT,
    finished_at    TEXT
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
    conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; far fewer disk flushes
    conn.execute("PRAGMA busy_timeout = 30000")  # dashboard, renders and Claude threads write at once
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first databases were created: (table, column, type).
ADDED_COLUMNS = [
    ("assets", "fingerprint", "BLOB"),
    ("scenes", "sorted", "INTEGER NOT NULL DEFAULT 0"),  # 1 = categorize has looked at it
    ("shorts", "uploaded_at", "TEXT"),                     # for the shared daily upload quota
    ("shorts", "edited", "INTEGER NOT NULL DEFAULT 0"),    # text changed by hand in review
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created."""
    changed = False
    for table, col, typ in ADDED_COLUMNS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            changed = True
    if changed:
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
