"""NASA Image and Video Library (no API key needed). US government work: public domain, except
contractor and third-party footage, which the credit check below flags.

API docs: https://images.nasa.gov/docs/images.nasa.gov_api_docs.pdf
"""
from __future__ import annotations

import re
from urllib.parse import quote

import requests

from . import Item, Rights
from .common import UA

NAME = "nasa"
LABEL = "NASA Image and Video Library"
LICENSE = "US government work, public domain (contractor/third-party footage excluded)"
NEEDS_KEY = False
HERO_OK = True
FIXED_CATEGORIES = ["space"]

API = "https://images-api.nasa.gov"

# Preferred renditions, best first. ~orig can be multi-GB ProRes, so it comes after ~large.
RENDITIONS = ("~large.mp4", "~orig.mp4", "~medium.mp4")

# Signs that (part of) the item is not NASA's own work and may not be public domain.
THIRD_PARTY = re.compile(
    r"courtesy|copyright|©|\(c\)|all rights reserved|spacex|blue origin|boeing|"
    r"lockheed|northrop|roscosmos|jaxa|\besa\b|getty|associated press|\bap\b|reuters|"
    r"rocket ?lab|united launch alliance|\bula\b|arianespace|virgin (galactic|orbit)|"
    r"sierra (space|nevada)|firefly|axiom|intuitive machines|astrobotic|relativity space|"
    r"used with permission",
    re.IGNORECASE,
)


def credit_of(data: dict) -> str:
    parts = [data.get("center"), data.get("photographer"), data.get("secondary_creator")]
    return " / ".join(p for p in parts if p)


def needs_rights_check(data: dict) -> bool:
    text = " ".join(str(data.get(k, "")) for k in ("photographer", "secondary_creator", "description", "title"))
    return bool(THIRD_PARTY.search(text))


def to_item(data: dict) -> Item:
    nasa_id = data["nasa_id"]
    return Item(
        source=NAME, source_id=nasa_id, title=data.get("title") or "",
        description=data.get("description") or data.get("description_508") or "",
        keywords=list(data.get("keywords") or []), creator=credit_of(data),
        source_url=f"https://images.nasa.gov/details/{quote(nasa_id)}",
        date_created=data.get("date_created"),
        extra={"photographer": data.get("photographer") or "",
               "secondary_creator": data.get("secondary_creator") or ""},
    )


def search(query: str, page: int = 1, session: requests.Session | None = None, key: str | None = None) -> list[Item]:
    s = session or requests.Session()
    r = s.get(f"{API}/search", params={"q": query, "media_type": "video", "page": page,
                                       "page_size": 100}, headers=UA, timeout=60)
    r.raise_for_status()
    items = []
    for item in r.json().get("collection", {}).get("items", []):
        data = (item.get("data") or [{}])[0]
        if data.get("nasa_id"):
            items.append(to_item(data))
    return items


def rights(item: Item) -> Rights:
    flagged = needs_rights_check({"title": item.title, "description": item.description,
                                  "photographer": item.extra.get("photographer", item.creator),
                                  "secondary_creator": item.extra.get("secondary_creator", "")})
    return Rights(license="public-domain", needs_check=flagged)


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


def file_url(item: Item, session: requests.Session | None = None, key: str | None = None) -> str | None:
    return pick_rendition(asset_files(item.source_id, session))


def upsert_metadata(conn, data: dict) -> int | None:
    """Store a NASA search result (raw API dict). Flagged items are kept usable-but-flagged
    (needs_check), as before the sources framework. Returns the asset id, or None if known."""
    from .common import upsert_item
    item = to_item(data)
    return upsert_item(conn, item, rights(item))
