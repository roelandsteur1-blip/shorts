"""Library of Congress film and video (Free to Use and Reuse sets, Paper Print films 1894-1915,
National Film Registry public-domain titles). JSON API, no key: add ?fo=json to any loc.gov page.

Only items the Library marks as free to use / no known restrictions / public domain are used.
"""
from __future__ import annotations

import re

import requests

from . import Item, Rights, as_list, strip_html
from .common import UA

NAME = "loc"
LABEL = "Library of Congress"
LICENSE = "public domain / no known restrictions only"
NEEDS_KEY = False
HERO_OK = True
MIN_HEIGHT = 360  # lots of silent, low-resolution film

SEARCH = "https://www.loc.gov/film-and-videos/"
FREE = re.compile(r"no known restrictions|public domain|free to use|not known to be restricted", re.I)
RESTRICTED = re.compile(r"rights? (are )?restricted|copyright(ed)? (by|holder)|permission (is )?required|"
                        r"in copyright|may be protected", re.I)


def search(query: str, page: int = 1, session: requests.Session | None = None, key: str | None = None) -> list[Item]:
    s = session or requests.Session()
    r = s.get(SEARCH, params={"q": query, "fo": "json", "c": 50, "sp": page,
                              "fa": "online-format:video"}, headers=UA, timeout=60)
    r.raise_for_status()
    items = []
    for d in r.json().get("results", []):
        url = d.get("id") or d.get("url") or ""
        m = re.search(r"/item/([^/]+)/?", url)
        if not m:
            continue
        items.append(Item(
            source=NAME, source_id=m.group(1), title=strip_html(d.get("title")),
            description=strip_html(d.get("description"))[:5000],
            keywords=as_list(d.get("subject")) + as_list(d.get("partof")),
            creator=strip_html(d.get("contributor")), source_url=url.replace("http://", "https://", 1),
            date_created=str(d.get("date") or "")[:10] or None,
            rights_text=strip_html(d.get("rights") or d.get("rights_advisory")),
        ))
    return items


def enrich(item: Item, session: requests.Session | None = None, key: str | None = None) -> Item:
    """The item page has the rights statement and the video files."""
    s = session or requests.Session()
    r = s.get(f"https://www.loc.gov/item/{item.source_id}/", params={"fo": "json"}, headers=UA, timeout=60)
    r.raise_for_status()
    data = r.json()
    it = data.get("item") or {}
    item.rights_text = " ".join(filter(None, [
        strip_html(it.get("rights_advisory")), strip_html(it.get("rights")),
        strip_html(it.get("rights_information")), item.rights_text]))
    item.keywords = list(dict.fromkeys(item.keywords + as_list(it.get("subjects") or it.get("subject_headings"))))
    item.file_url = pick_file(data.get("resources") or [])
    return item


def pick_file(resources: list[dict]) -> str | None:
    best = None
    for res in resources:
        for group in res.get("files") or []:
            for f in group if isinstance(group, list) else [group]:
                if (f.get("mimetype") or "").startswith("video/mp4") and f.get("url"):
                    rank = (int(f.get("height") or 0), int(f.get("size") or 0))
                    if best is None or rank > best[0]:
                        best = (rank, f["url"])
    return best[1] if best else None


def rights(item: Item) -> Rights:
    text = item.rights_text or ""
    free_set = any("free to use" in k.lower() for k in item.keywords)
    if RESTRICTED.search(text):
        return Rights(reject=f"rights restricted: {text[:120]}")
    if FREE.search(text) or free_set:
        return Rights(license="public-domain")
    return Rights(reject="not marked free to use / no known restrictions")


def file_url(item: Item, session: requests.Session | None = None, key: str | None = None) -> str | None:
    return item.file_url or enrich(item, session).file_url
