"""Pixabay stock video (free API key). Pixabay Content License: free, no credit needed. No model or
property releases, so logos and recognisable people are our risk. Used between text blocks only."""
from __future__ import annotations

import requests

from . import Item, Rights
from .common import UA

NAME = "pixabay"
LABEL = "Pixabay"
LICENSE = "Pixabay Content License (free, no credit needed)"
NOTE = "No model/property releases: logos and recognisable people are our risk."
NEEDS_KEY = True
KEY_URL = "https://pixabay.com/api/docs/"
KEY_HELP = "Log in (free account); the Pixabay API docs page then shows your key under 'Parameters' (key)."
HERO_OK = False
API = "https://pixabay.com/api/videos/"


def search(query: str, page: int = 1, session: requests.Session | None = None, key: str | None = None) -> list[Item]:
    if not key:
        raise ValueError("Pixabay needs an API key (Sources page)")
    s = session or requests.Session()
    r = s.get(API, params={"key": key, "q": query[:100], "per_page": 50, "page": page, "safesearch": "true"},
              headers=UA, timeout=60)
    r.raise_for_status()
    items = []
    for h in r.json().get("hits") or []:
        vids = h.get("videos") or {}
        best = next((vids[k] for k in ("large", "medium", "small") if (vids.get(k) or {}).get("url")), None)
        tags = [t.strip() for t in (h.get("tags") or "").split(",") if t.strip()]
        items.append(Item(source=NAME, source_id=str(h["id"]), title=", ".join(tags[:4]) or f"Pixabay video {h['id']}",
                          keywords=tags, creator=h.get("user") or "", source_url=h.get("pageURL") or "",
                          file_url=best["url"] if best else None))
    return items


def rights(item: Item) -> Rights:
    if not item.file_url:
        return Rights(reject="no video file")
    return Rights(license="Pixabay Content License")


def file_url(item: Item, session: requests.Session | None = None, key: str | None = None) -> str | None:
    return item.file_url
