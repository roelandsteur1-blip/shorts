"""Pexels stock video (free API key). Pexels License: free to use, no credit needed (we name it
anyway). Generic B-roll with no story to explain, so it's used between text blocks, never as the
hero shot. Its API terms forbid AI training: we only search with CLIP (no training), noted here."""
from __future__ import annotations

import re

import requests

from . import Item, Rights
from .common import UA

NAME = "pexels"
LABEL = "Pexels"
LICENSE = "Pexels License (free, no credit needed)"
NOTE = "API terms forbid AI training (we only search with CLIP); no brand endorsement or negative context for people."
NEEDS_KEY = True
KEY_URL = "https://www.pexels.com/api/"
HERO_OK = False
API = "https://api.pexels.com/videos/search"


def search(query: str, page: int = 1, session: requests.Session | None = None, key: str | None = None) -> list[Item]:
    if not key:
        raise ValueError("Pexels needs an API key (Sources page)")
    s = session or requests.Session()
    r = s.get(API, params={"query": query, "per_page": 40, "page": page},
              headers={**UA, "Authorization": key}, timeout=60)
    r.raise_for_status()
    items = []
    for v in r.json().get("videos") or []:
        slug = re.search(r"/video/([^/]+?)-?\d*/?$", v.get("url") or "")
        title = slug.group(1).replace("-", " ").strip() if slug else f"Pexels video {v['id']}"
        files = [f for f in v.get("video_files") or [] if f.get("file_type") == "video/mp4" and f.get("link")
                 and (f.get("height") or 0) <= 2160]
        best = max(files, key=lambda f: (f.get("height") or 0) * (f.get("width") or 0), default=None)
        items.append(Item(source=NAME, source_id=str(v["id"]), title=title.capitalize(),
                          keywords=[t if isinstance(t, str) else t.get("name", "") for t in v.get("tags") or []],
                          creator=(v.get("user") or {}).get("name", ""), source_url=v.get("url") or "",
                          file_url=best["link"] if best else None))
    return items


def rights(item: Item) -> Rights:
    if not item.file_url:
        return Rights(reject="no mp4 file")
    return Rights(license="Pexels License")


def file_url(item: Item, session: requests.Session | None = None, key: str | None = None) -> str | None:
    return item.file_url
