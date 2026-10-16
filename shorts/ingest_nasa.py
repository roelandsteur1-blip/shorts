"""Grow the footage library from the NASA Image and Video Library (no API key needed).

API docs: https://images.nasa.gov/docs/images.nasa.gov_api_docs.pdf
"""
from __future__ import annotations

import logging
import re
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from . import media
from .config import Settings

log = logging.getLogger(__name__)

API = "https://images-api.nasa.gov"
UA = {"User-Agent": "shorts-pipeline/0.1 (personal project)"}

# Preferred renditions, best first. ~orig can be multi-GB ProRes, so it comes after ~large.
RENDITIONS = ("~large.mp4", "~orig.mp4", "~medium.mp4")
MAX_BYTES = 2_500_000_000
MIN_SECONDS = 4
MAX_SECONDS = 40 * 60

# Signs that (part of) the item is not NASA's own work and may not be public domain.
THIRD_PARTY = re.compile(
    r"courtesy|copyright|©|\(c\)|all rights reserved|spacex|blue origin|boeing|"
    r"lockheed|northrop|roscosmos|jaxa|\besa\b|getty|associated press|\bap\b|reuters|"
    r"used with permission",
    re.IGNORECASE,
)


def _safe_url(url: str) -> str:
    parts = urlsplit(url.replace("http://", "https://", 1))
    return urlunsplit(parts._replace(path=quote(parts.path)))


def search(term: str, page: int = 1, page_size: int = 100, session: requests.Session | None = None) -> list[dict]:
    s = session or requests.Session()
    r = s.get(f"{API}/search", params={"q": term, "media_type": "video", "page": page,
                                       "page_size": page_size}, headers=UA, timeout=60)
    r.raise_for_status()
    items = []
    for item in r.json().get("collection", {}).get("items", []):
        data = (item.get("data") or [{}])[0]
        if data.get("nasa_id"):
            items.append(data)
    return items


def asset_files(nasa_id: str, session: requests.Session | None = None) -> list[str]:
    s = session or requests.Session()
    r = s.get(f"{API}/asset/{quote(nasa_id)}", headers=UA, timeout=60)
    r.raise_for_status()
    return [i["href"] for i in r.json().get("collection", {}).get("items", [])]


def pick_rendition(files: list[str]) -> str | None:
    for suffix in RENDITIONS:
        for f in files:
            if f.lower().endswith(suffix):
                return f
    return None


def credit_of(data: dict) -> str:
    parts = [data.get("center"), data.get("photographer"), data.get("secondary_creator")]
    return " / ".join(p for p in parts if p)


def needs_rights_check(data: dict) -> bool:
    text = " ".join(str(data.get(k, "")) for k in ("photographer", "secondary_creator", "description", "title"))
    return bool(THIRD_PARTY.search(text))


def _download(url: str, dst: Path, session: requests.Session) -> None:
    with session.get(_safe_url(url), headers=UA, stream=True, timeout=120) as r:
        r.raise_for_status()
        size = int(r.headers.get("Content-Length") or 0)
        if size > MAX_BYTES:
            raise ValueError(f"file too large ({size / 1e9:.1f} GB)")
        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def upsert_metadata(conn: sqlite3.Connection, data: dict) -> int | None:
    """Store an item's metadata. Returns the asset id, or None if it already existed."""
    nasa_id = data["nasa_id"]
    row = conn.execute("SELECT id FROM assets WHERE source='nasa' AND source_id=?", (nasa_id,)).fetchone()
    if row:
        return None
    cur = conn.execute(
        """INSERT INTO assets (source, source_id, title, description, keywords, credit, license,
                               needs_check, source_url, date_created)
           VALUES ('nasa', ?, ?, ?, ?, ?, 'public-domain', ?, ?, ?)""",
        (
            nasa_id,
            data.get("title"),
            data.get("description") or data.get("description_508"),
            ", ".join(data.get("keywords") or []),
            credit_of(data),
            int(needs_rights_check(data)),
            f"https://images.nasa.gov/details/{quote(nasa_id)}",
            data.get("date_created"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def download_asset(conn: sqlite3.Connection, settings: Settings, asset_id: int,
                   session: requests.Session | None = None) -> bool:
    s = session or requests.Session()
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    try:
        url = pick_rendition(asset_files(row["source_id"], s))
        if not url:
            raise ValueError("no mp4 rendition")
        dst = settings.footage_dir / "nasa" / f"{_slug(row['source_id'])}.mp4"
        with tempfile.TemporaryDirectory(dir=settings.data_dir) as tmp:
            raw = Path(tmp) / "raw.mp4"
            _download(url, raw, s)
            info = media.probe(raw)
            if not MIN_SECONDS <= info["duration"] <= MAX_SECONDS:
                raise ValueError(f"duration {info['duration']:.0f}s out of range")
            if info["height"] < 720:
                raise ValueError(f"resolution too low ({info['width']}x{info['height']})")
            media.strip_audio(raw, dst)
        conn.execute(
            """UPDATE assets SET file_url=?, path=?, duration=?, width=?, height=?,
                      status='downloaded', error=NULL WHERE id=?""",
            (url, str(dst), info["duration"], info["width"], info["height"], asset_id),
        )
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001 - record and continue with the next item
        log.warning("asset %s failed: %s", row["source_id"], e)
        conn.execute("UPDATE assets SET status='failed', error=? WHERE id=?", (str(e)[:500], asset_id))
        conn.commit()
        return False


def ingest(conn: sqlite3.Connection, settings: Settings, terms: list[str], per_term: int = 10,
           include_flagged: bool = False) -> int:
    """Search each term and download up to `per_term` new videos. Returns number downloaded."""
    settings.ensure_dirs()
    s = requests.Session()
    total = 0
    for term in terms:
        got = 0
        page = 1
        while got < per_term and page <= 5:
            items = search(term, page=page, session=s)
            if not items:
                break
            for data in items:
                if got >= per_term:
                    break
                if needs_rights_check(data) and not include_flagged:
                    continue
                asset_id = upsert_metadata(conn, data)
                if asset_id is None:
                    continue
                log.info("[%s] downloading %s", term, data["nasa_id"])
                if download_asset(conn, settings, asset_id, s):
                    got += 1
            page += 1
        log.info("[%s] %d new videos", term, got)
        total += got
    return total


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:120]
