"""Internet Archive movies (incl. '50 Years of NASA' and public-domain films). No key needed.

Rights vary per item and uploaders often mislabel them, so only items whose `licenseurl` is a
license we recognise are used (public domain mark, CC0, CC BY). Everything else is rejected.
APIs: https://archive.org/advancedsearch.php and https://archive.org/metadata/<identifier>
"""
from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import quote

import requests

from . import Item, Rights, as_list, cc_by_attribution, license_from_url, strip_html
from .common import MAX_BYTES, UA

NAME = "internet_archive"
LABEL = "Internet Archive"
LICENSE = "varies per item: only public domain, CC0 and CC BY items are used"
NEEDS_KEY = False
HERO_OK = True
MIN_HEIGHT = 360  # old film

SEARCH = "https://archive.org/advancedsearch.php"
FIELDS = ["identifier", "title", "description", "subject", "licenseurl", "date", "creator", "collection"]
BASE_FILTER = "mediatype:(movies) AND -collection:(prelinger)"  # Prelinger is its own source
VIDEO_EXT = (".mp4", ".m4v", ".mov", ".mkv", ".mpeg", ".mpg", ".avi", ".ogv", ".webm")


def build_query(query: str, base: str = BASE_FILTER) -> str:
    # Category search hints are already in archive syntax ("subject:(war) AND ..."); plain terms are not.
    return f"({query}) AND {base}" if query else base


def search(query: str, page: int = 1, session: requests.Session | None = None, key: str | None = None,
           base: str = BASE_FILTER, source: str = NAME) -> list[Item]:
    s = session or requests.Session()
    params = [("q", build_query(query, base)), ("rows", "50"), ("page", str(page)), ("output", "json")]
    params += [("fl[]", f) for f in FIELDS]
    r = s.get(SEARCH, params=params, headers=UA, timeout=60)
    r.raise_for_status()
    return [to_item(d, source) for d in r.json().get("response", {}).get("docs", []) if d.get("identifier")]


def to_item(d: dict, source: str = NAME) -> Item:
    ident = d["identifier"]
    title = d.get("title") or ident
    return Item(
        source=source, source_id=ident, title=strip_html(title),
        description=strip_html(d.get("description"))[:5000],
        keywords=as_list(d.get("subject")) + [f"collection:{c}" for c in as_list(d.get("collection"))],
        creator=strip_html(d.get("creator")), source_url=f"https://archive.org/details/{quote(ident)}",
        date_created=str(d.get("date") or "")[:10] or None,
        license_hint=str(d.get("licenseurl") or ""),
    )


def rights(item: Item) -> Rights:
    r = license_from_url(item.license_hint)
    return cc_by_attribution(r, item.creator, "Internet Archive")


def pick_file(files: list[dict]) -> str | None:
    """Best video file of an item: mp4 first (no re-encode), then the largest that fits."""
    videos = []
    for f in files:
        name = f.get("name") or ""
        if PurePosixPath(name.lower()).suffix not in VIDEO_EXT:
            continue
        size = int(f.get("size") or 0)
        if size > MAX_BYTES:
            continue
        videos.append((name.lower().endswith(".mp4"), int(f.get("height") or 0), size, name))
    if not videos:
        return None
    return max(videos)[3]


def file_url(item: Item, session: requests.Session | None = None, key: str | None = None) -> str | None:
    s = session or requests.Session()
    r = s.get(f"https://archive.org/metadata/{quote(item.source_id)}", headers=UA, timeout=60)
    r.raise_for_status()
    name = pick_file(r.json().get("files") or [])
    return f"https://archive.org/download/{quote(item.source_id)}/{quote(name)}" if name else None
