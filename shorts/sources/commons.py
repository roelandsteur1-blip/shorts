"""Wikimedia Commons videos via the MediaWiki API (no key). License per file, from its
machine-readable metadata: public domain, CC0 and CC BY are used (CC BY needs a credit line);
CC BY-SA and anything without a recognised license are rejected."""
from __future__ import annotations

import re

import requests

from . import Item, Rights, as_list, cc_by_attribution, license_from_name, strip_html
from .common import MAX_BYTES, MAX_SECONDS, MIN_SECONDS, UA

NAME = "commons"
LABEL = "Wikimedia Commons"
LICENSE = "per file: public domain, CC0, CC BY (credit needed); CC BY-SA skipped"
NEEDS_KEY = False
HERO_OK = True
MIN_HEIGHT = 480

API = "https://commons.wikimedia.org/w/api.php"
PAGE = 50
META = "LicenseShortName|License|Artist|Credit|ImageDescription|Categories|DateTimeOriginal|ObjectName"


def search(query: str, page: int = 1, session: requests.Session | None = None, key: str | None = None) -> list[Item]:
    s = session or requests.Session()
    r = s.get(API, params={
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:video", "gsrnamespace": 6, "gsrlimit": PAGE,
        "gsroffset": (page - 1) * PAGE, "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiextmetadatafilter": META,
    }, headers=UA, timeout=60)
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}
    items = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (p.get("imageinfo") or [{}])[0]
        if not info.get("url") or not str(info.get("mime", "")).startswith("video/"):
            continue
        meta = {k: (v or {}).get("value", "") for k, v in (info.get("extmetadata") or {}).items()}
        title = p.get("title", "").removeprefix("File:")
        items.append(Item(
            source=NAME, source_id=str(p.get("pageid")),
            title=strip_html(meta.get("ObjectName")) or title.rsplit(".", 1)[0],
            description=strip_html(meta.get("ImageDescription"))[:5000],
            keywords=as_list(meta.get("Categories")), creator=strip_html(meta.get("Artist"))[:200],
            source_url=info.get("descriptionurl") or "", date_created=strip_html(meta.get("DateTimeOriginal"))[:10] or None,
            license_hint=meta.get("License") or meta.get("LicenseShortName") or "",
            file_url=pick_url(info), extra={"short_name": meta.get("LicenseShortName", ""),
                                            "duration": info.get("duration")},
        ))
    return items


def pick_url(info: dict) -> str:
    """The original file, or Commons' own 1080p transcode when the original is over 1080p or too
    large to download (4K NASA uploads run to several GB)."""
    url = info["url"]
    if int(info.get("height") or 0) <= 1080 and int(info.get("size") or 0) <= MAX_BYTES:
        return url
    m = re.match(r"(https://upload\.wikimedia\.org/wikipedia/commons)/(\w/\w\w/)([^/]+)$", url)
    return f"{m.group(1)}/transcoded/{m.group(2)}{m.group(3)}/{m.group(3)}.1080p.vp9.webm" if m else url


def rights(item: Item) -> Rights:
    r = license_from_name(item.license_hint)
    if r.reject and item.extra.get("short_name"):
        r = license_from_name(item.extra["short_name"])
    seconds = item.extra.get("duration")
    if not r.reject and seconds is not None and not MIN_SECONDS <= float(seconds) <= MAX_SECONDS:
        r.reject = f"duration {float(seconds):.0f}s out of range"
    return cc_by_attribution(r, item.creator, "Wikimedia Commons")


def file_url(item: Item, session: requests.Session | None = None, key: str | None = None) -> str | None:
    return item.file_url
