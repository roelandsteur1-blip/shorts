"""What every source shares: store an item, check its rights, download it, strip the audio,
skip duplicates. Source modules only search, judge rights and name the file (see __init__)."""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from .. import dedupe, media, progress, timing
from ..config import Settings
from . import Item, Rights, api_key, get

log = logging.getLogger(__name__)

# Wikimedia (and others) ask for a contact link in the User-Agent and rate-limit requests without one.
UA = {"User-Agent": "shorts-pipeline/0.2 (https://github.com/roelandsteur1-blip/shorts; "
                    "personal project; public-domain footage for YouTube Shorts)"}
MAX_BYTES = 2_500_000_000
MIN_SECONDS = 4
MAX_SECONDS = 40 * 60
MIN_HEIGHT = 720  # sources with old film (Prelinger, LoC, ...) set their own, lower MIN_HEIGHT


def _safe_url(url: str) -> str:
    parts = urlsplit(url.replace("http://", "https://", 1))
    return urlunsplit(parts._replace(path=quote(parts.path, safe="/%~:@!$&'()*+,;=")))


def _download(url: str, dst: Path, session: requests.Session) -> None:
    with session.get(_safe_url(url), headers=UA, stream=True, timeout=120) as r:
        r.raise_for_status()
        size = int(r.headers.get("Content-Length") or 0)
        if size > MAX_BYTES:
            raise ValueError(f"file too large ({size / 1e9:.1f} GB)")
        got = 0
        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                got += len(chunk)
                if got > MAX_BYTES:
                    raise ValueError("file too large")
                f.write(chunk)


def exists(conn: sqlite3.Connection, source: str, source_id: str) -> bool:
    return conn.execute("SELECT 1 FROM assets WHERE source=? AND source_id=?", (source, source_id)).fetchone() is not None


def upsert_item(conn: sqlite3.Connection, item: Item, rights: Rights) -> int | None:
    """Store an item's metadata and rights. Returns the asset id, or None if it already existed.
    Rejected items are stored too (status 'rejected'), so they are counted and never re-checked."""
    if exists(conn, item.source, item.source_id):
        return None
    cur = conn.execute(
        """INSERT INTO assets (source, source_id, title, description, keywords, credit, license,
                               attribution, needs_check, source_url, file_url, date_created, status, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (item.source, item.source_id, item.title, item.description, ", ".join(item.keywords),
         item.creator, rights.license or "unknown", rights.attribution, int(rights.needs_check),
         item.source_url, item.file_url, item.date_created,
         "rejected" if rights.reject else "new", rights.reject),
    )
    conn.commit()
    return cur.lastrowid


def item_from_row(row: sqlite3.Row) -> Item:
    return Item(source=row["source"], source_id=row["source_id"], title=row["title"] or "",
                description=row["description"] or "", source_url=row["source_url"] or "",
                file_url=row["file_url"], date_created=row["date_created"])


def download_asset(conn: sqlite3.Connection, settings: Settings, asset_id: int,
                   session: requests.Session | None = None) -> bool:
    s = session or requests.Session()
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    # Downloads run in parallel: the duplicate checks and claiming a title happen one at a time.
    with _dedupe_lock:
        orig = dedupe.same_title(conn, row["title"], asset_id, row["date_created"], ignore_new_after=asset_id)
        if orig is not None:
            log.info("skip %s: same title as asset %s", row["source_id"], orig)
            dedupe.mark_duplicate(conn, asset_id, orig, "same title")
            return False
        conn.execute("UPDATE assets SET status='downloading' WHERE id=?", (asset_id,))
        conn.commit()
    mod = get(row["source"])
    t0 = time.perf_counter()
    try:
        url = row["file_url"] or mod.file_url(item_from_row(row), s, api_key(settings, row["source"]))
        if not url:
            raise ValueError("no video file")
        dst = settings.footage_dir / row["source"] / f"{_slug(row['source_id'])}.mp4"
        # Temp download on the footage drive: raw files can be GBs.
        with tempfile.TemporaryDirectory(dir=settings.footage_dir) as tmp:
            raw = Path(tmp) / ("raw" + (Path(urlsplit(url).path).suffix[:6] or ".mp4"))
            _download(url, raw, s)
            t_download = time.perf_counter() - t0
            info = media.probe(raw)
            if not MIN_SECONDS <= info["duration"] <= MAX_SECONDS:
                raise ValueError(f"duration {info['duration']:.0f}s out of range")
            min_height = getattr(mod, "MIN_HEIGHT", MIN_HEIGHT)
            if info["height"] < min_height:
                raise ValueError(f"resolution too low ({info['width']}x{info['height']})")
            t1 = time.perf_counter()
            fp = dedupe.fingerprint(str(raw))
            timing.record(conn, "fingerprint", time.perf_counter() - t1, asset=asset_id,
                          video_seconds=round(info["duration"]), height=info["height"])
            with _dedupe_lock:
                if fp and (orig := dedupe.same_footage(conn, fp, asset_id)) is not None:
                    log.info("skip %s: same footage as asset %s", row["source_id"], orig)
                    dedupe.mark_duplicate(conn, asset_id, orig, "same footage")
                    return False
                if fp:  # visible to the other downloads' check right away
                    conn.execute("UPDATE assets SET fingerprint=? WHERE id=?", (fp, asset_id))
                    conn.commit()
            media.strip_audio(raw, dst)
        conn.execute(
            """UPDATE assets SET file_url=?, path=?, duration=?, width=?, height=?, fingerprint=?,
                      status='downloaded', error=NULL WHERE id=?""",
            (url, str(dst), info["duration"], info["width"], info["height"], fp, asset_id),
        )
        conn.commit()
        timing.record(conn, "download", t_download, asset=asset_id, source=row["source"],
                      mb=round(dst.stat().st_size / 1e6))
        return True
    except Exception as e:  # noqa: BLE001 - record and continue with the next item
        log.warning("asset %s failed: %s", row["source_id"], e)
        conn.execute("UPDATE assets SET status='failed', error=? WHERE id=?", (str(e)[:500], asset_id))
        conn.commit()
        return False


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def check(item: Item, session: requests.Session, key: str | None) -> tuple[Item, Rights]:
    """Fetch details if the source needs them, then judge the rights."""
    mod = get(item.source)
    if hasattr(mod, "enrich"):
        item = mod.enrich(item, session, key)
    return item, mod.rights(item)


# Files downloaded at once per source: archives that ask for few connections get fewer.
POLITE = {"commons": 2, "internet_archive": 2, "prelinger": 2, "europeana": 2, "loc": 1, "openbeelden": 1}

_dedupe_lock = threading.Lock()
_local = threading.local()


def _download_in_thread(settings: Settings, asset_id: int) -> bool:
    """download_asset with this thread's own database connection and HTTP session."""
    if getattr(_local, "conn", None) is None or getattr(_local, "db", None) != settings.db_path:
        from .. import db
        _local.conn, _local.db, _local.session = db.connect(settings.db_path), settings.db_path, requests.Session()
    return download_asset(_local.conn, settings, asset_id, _local.session)


def ingest(conn: sqlite3.Connection, settings: Settings, source: str, queries: list[str],
           per_term: int = 10, include_flagged: bool = False, max_pages: int = 10) -> int:
    """Search each query on one source and download up to `per_term` new videos per query, several
    files at once (Quiet/Fast switch, and fewer for archives that ask for it). Returns the number
    downloaded."""
    from .. import speed
    settings.ensure_dirs()
    mod = get(source)
    key = api_key(settings, source)
    s = requests.Session()
    gate = speed.Gate(settings.data_dir, "downloads")
    polite = POLITE.get(source, 4)
    total = 0
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix=f"dl-{source}") as pool:
        for term in queries:
            got = 0
            page = 1
            running: set = set()

            def collect(block: bool) -> None:
                nonlocal got, total
                if not running:
                    return
                finished, _ = wait(running, timeout=None if block else 0, return_when=FIRST_COMPLETED)
                for f in finished:
                    running.discard(f)
                    try:
                        ok = f.result()
                    except Exception as e:  # noqa: BLE001
                        log.warning("[%s] download failed: %s", source, e)
                        ok = False
                    if ok:
                        got += 1
                        total += 1
                        progress.report(total, len(queries) * per_term, f"download {source}")

            while got + len(running) < per_term and page <= max_pages:
                if (free := free_gb(settings.footage_dir)) < settings.min_free_gb:
                    log.warning("only %.0f GB free on %s (min_free_gb = %s): stopped downloading; "
                                "run 'python -m shorts cleanup'", free, settings.footage_dir, settings.min_free_gb)
                    while running:
                        collect(block=True)
                    return total
                try:
                    items = mod.search(term, page=page, session=s, key=key)
                except (requests.RequestException, ValueError) as e:  # ValueError: missing key, bad reply
                    log.warning("[%s] search '%s' failed: %s", source, term, e)
                    break
                if not items:
                    break
                for item in items:
                    collect(block=False)
                    if got + len(running) >= per_term:
                        break
                    if exists(conn, item.source, item.source_id):
                        continue
                    try:
                        item, r = check(item, s, key)
                    except requests.RequestException as e:
                        log.warning("[%s] details for %s failed: %s", source, item.source_id, e)
                        continue
                    if r.needs_check and not include_flagged and not r.reject:
                        r.reject = "rights check: credit is not plainly the source's own"
                    asset_id = upsert_item(conn, item, r)
                    if asset_id is None or r.reject:
                        if r.reject:
                            log.info("[%s] skip %s: %s", source, item.source_id, r.reject)
                        continue
                    while len(running) >= min(gate.allowed(), polite):
                        collect(block=True)
                    log.info("[%s] [%s] downloading %s", source, term, item.source_id)
                    running.add(pool.submit(_download_in_thread, settings, asset_id))
                page += 1
                # A page's downloads may fail (duplicates, too short): wait before searching on.
                while running and got + len(running) >= per_term:
                    collect(block=True)
            while running:
                collect(block=True)
            log.info("[%s] [%s] %d new videos", source, term, got)
    return total


def recheck_rights(conn: sqlite3.Connection) -> int:
    """Re-apply the NASA third-party check to footage already in the library (the rules grow over
    time). Only ever adds flags; clearing one is a manual decision. Returns newly flagged count."""
    from .nasa import needs_rights_check
    n = 0
    for r in conn.execute("""SELECT id, title, description, credit FROM assets
                              WHERE needs_check = 0 AND source = 'nasa'""").fetchall():
        if needs_rights_check({"title": r["title"], "description": r["description"],
                               "photographer": r["credit"]}):
            conn.execute("UPDATE assets SET needs_check = 1 WHERE id = ?", (r["id"],))
            n += 1
    conn.commit()
    return n


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:120]
