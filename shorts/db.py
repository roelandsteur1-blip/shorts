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

-- Every video on each channel as YouTube reports it (ytstats.py), updated in place on every fetch.
-- state: live (public/unlisted), scheduled, private, processing, failed, gone (no longer on YouTube).
-- views/likes/comments are live (Data API); an_views..subs_gained come from Analytics, which lags
-- 1-3 days: NULL = not in Analytics yet. short_id is NULL for videos the pipeline didn't make.
CREATE TABLE IF NOT EXISTS yt_videos (
    youtube_id     TEXT PRIMARY KEY,
    channel        TEXT NOT NULL,
    short_id       INTEGER,
    title          TEXT,
    state          TEXT NOT NULL,
    privacy        TEXT,
    publish_at     TEXT,
    duration_s     REAL,
    is_short       INTEGER NOT NULL DEFAULT 1,     -- 3 minutes or less
    views          INTEGER,
    likes          INTEGER,
    comments       INTEGER,
    an_views       INTEGER,
    engaged_views  INTEGER,
    avg_view_pct   REAL,
    avg_view_dur   REAL,
    shares         INTEGER,
    subs_gained    INTEGER,
    hidden         INTEGER NOT NULL DEFAULT 0,     -- Hide button on Stats
    first_seen     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen      TEXT
);

-- Live views per video per day (the last fetch of that day), for growth without waiting for Analytics.
CREATE TABLE IF NOT EXISTS yt_daily (
    youtube_id  TEXT NOT NULL,
    day         TEXT NOT NULL,
    views       INTEGER,
    PRIMARY KEY (youtube_id, day)
);

-- Last stats fetch per channel: when it last worked, the last error, and how far Analytics has data.
CREATE TABLE IF NOT EXISTS yt_sync (
    channel            TEXT PRIMARY KEY,
    ok_at              TEXT,
    error              TEXT,
    error_at           TEXT,
    analytics_through  TEXT,
    yt_channel_id      TEXT
);

-- Each Short's first 1, 3 and 7 days (YouTube Analytics, fetched once the days are final), so
-- channels and Shorts are compared at the same age instead of by lifetime totals.
CREATE TABLE IF NOT EXISTS stats_age (
    short_id       INTEGER NOT NULL REFERENCES shorts(id),
    days           INTEGER NOT NULL,
    views          INTEGER,
    engaged_views  INTEGER,
    avg_view_pct   REAL,
    avg_view_dur   REAL,
    likes          INTEGER,
    shares         INTEGER,
    subs_gained    INTEGER,
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (short_id, days)
);

-- Audience retention over a Short's first 3 days (YouTube Analytics audienceWatchRatio per
-- elapsedVideoTimeRatio): JSON [[time share 0..1, share still watching], ...]. Fetched once.
CREATE TABLE IF NOT EXISTS retention (
    short_id    INTEGER PRIMARY KEY REFERENCES shorts(id),
    curve       TEXT NOT NULL,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
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

-- How long each step took (shorts/timing.py), for the Stats page.
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
    heavy          INTEGER NOT NULL DEFAULT 1, -- 1 = uses CPU/Claude: one at a time
    status         TEXT NOT NULL DEFAULT 'queued',
        -- queued, running, done, failed, stopped, interrupted (dashboard restarted mid-job)
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

-- Background music per channel folder (shorts/music.py).
CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY,
    folder       TEXT NOT NULL,              -- subfolder of data_dir/music ('' = loose files)
    path         TEXT UNIQUE,                -- NULL for rejected downloads
    source       TEXT NOT NULL,              -- openverse, freesound, folder (put there by hand)
    source_id    TEXT,
    title        TEXT,
    creator      TEXT,
    license      TEXT,                       -- 'CC0', 'public-domain', 'added by hand'
    source_url   TEXT,
    duration     REAL,
    start        REAL NOT NULL DEFAULT 0,    -- seconds: where the music gets going
    mood         TEXT,                       -- calm, epic, mysterious, upbeat, tense
    music_score  REAL,                       -- CLAP: how much it sounds like instrumental music
    analysed     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'ok', -- ok, rejected, claimed (by YouTube), missing
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, source_id)
);

-- Copyright (Content ID) claims on our Shorts. Claimed footage is never used again: public-domain
-- clips can't be in Content ID, but broadcasters whose programmes contain them do claim them.
CREATE TABLE IF NOT EXISTS claims (
    id        INTEGER PRIMARY KEY,
    short_id  INTEGER REFERENCES shorts(id),
    asset_id  INTEGER REFERENCES assets(id),
    claimant  TEXT,
    note      TEXT,
    at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Old: videos the script writer said don't fit a channel. Copied into channel_fit, no longer written.
CREATE TABLE IF NOT EXISTS off_niche (
    channel   TEXT NOT NULL,
    asset_id  INTEGER NOT NULL,
    reason    TEXT,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, asset_id)
);

-- Which videos fit which channel (shorts/fit.py): one Claude call sorts a batch of videos against
-- every channel's niche. fits 1/0; niche = the niche it was judged against (a changed niche sorts
-- again), '*' = stays whatever the niche (set by hand). method: claude, script (the script writer
-- skipped it), manual.
CREATE TABLE IF NOT EXISTS channel_fit (
    channel   TEXT NOT NULL,
    asset_id  INTEGER NOT NULL,
    fits      INTEGER NOT NULL,
    niche     TEXT NOT NULL,
    reason    TEXT,
    method    TEXT NOT NULL,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, asset_id)
);
CREATE INDEX IF NOT EXISTS channel_fit_asset ON channel_fit(asset_id);
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
    first_sync = not conn.execute("SELECT 1 FROM sqlite_master WHERE name='yt_videos'").fetchone()
    conn.executescript(SCHEMA)
    _migrate(conn)
    if first_sync:
        # Stats now follow YouTube: whatever is live on a channel counts, so the old "hidden" marks go.
        # They never stopped footage or topics being reused: a Short blocks reuse just by existing.
        conn.execute("UPDATE shorts SET stats_hidden = 0 WHERE youtube_id IS NOT NULL")
        conn.commit()
    return conn


# Columns added after the first databases were created: (table, column, type).
ADDED_COLUMNS = [
    ("assets", "fingerprint", "BLOB"),
    ("scenes", "sorted", "INTEGER NOT NULL DEFAULT 0"),  # 1 = categorize has looked at it
    ("shorts", "uploaded_at", "TEXT"),                     # for the shared daily upload quota
    ("shorts", "edited", "INTEGER NOT NULL DEFAULT 0"),    # text changed by hand in review
    ("shorts", "mood", "TEXT"),                            # music mood the script asked for
    ("shorts", "voice", "TEXT"),                           # Kokoro voice reading it; NULL = text only
    ("shorts", "stats_hidden", "INTEGER NOT NULL DEFAULT 0"),  # 1 = left out of Stats; still blocks reuse
    ("assets", "claimed", "TEXT"),                         # who claimed it (claims table); never used again
    ("shorts", "screen_title", "TEXT"),                    # title bar at the top of the video (render.py)
    ("stats", "live_views", "INTEGER"),                    # Data API view count at fetch time (Analytics lags days)
    ("stats", "live_likes", "INTEGER"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created."""
    changed = False
    for table, col, typ in ADDED_COLUMNS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:  # another process added it at the same moment
                if "duplicate column" not in str(e):
                    raise
            changed = True
    # Script-writer rejections from before channel_fit existed stay rejected.
    if conn.execute("SELECT 1 FROM off_niche LIMIT 1").fetchone():
        conn.execute("""INSERT OR IGNORE INTO channel_fit (channel, asset_id, fits, niche, reason, method, at)
                        SELECT channel, asset_id, 0, '*', reason, 'script', at FROM off_niche""")
        conn.execute("DELETE FROM off_niche")
        changed = True
    if changed:
        conn.commit()


def log_claim(conn: sqlite3.Connection, short_id: int, asset_ids: list[int], claimant: str, note: str = "") -> None:
    for aid in asset_ids:
        conn.execute("INSERT INTO claims (short_id, asset_id, claimant, note) VALUES (?,?,?,?)",
                     (short_id, aid, claimant, note))
        conn.execute("UPDATE assets SET claimed=? WHERE id=?", (claimant or "unknown", aid))
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


def used_scene_ids(conn: sqlite3.Connection, channel: str | None) -> set[int]:
    """Scenes already used by a non-rejected short on this channel (None: on any channel)."""
    rows = conn.execute(
        """
        SELECT s.hero_scene_id AS sid FROM shorts s
         WHERE (? IS NULL OR s.channel = ?) AND s.status != 'rejected' AND s.hero_scene_id IS NOT NULL
        UNION
        SELECT b.scene_id FROM blocks b JOIN shorts s ON s.id = b.short_id
         WHERE (? IS NULL OR s.channel = ?) AND s.status != 'rejected' AND b.scene_id IS NOT NULL
        """,
        (channel, channel, channel, channel),
    ).fetchall()
    return {r[0] for r in rows}
