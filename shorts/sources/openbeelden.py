"""Open Beelden (Beeld en Geluid: Polygoon newsreels and more) via its OAI-PMH feed (no key).

Mostly CC BY-SA, which is skipped, so only the public-domain (and CC BY) part is used: expect a
small yield. People in Dutch footage may have portrait rights; check close-ups in review.
OAI-PMH has no keyword search, so feed pages are read in order and filtered on the query words."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import requests

from . import Item, Rights, cc_by_attribution, license_from_url, strip_html
from .common import UA

NAME = "openbeelden"
LABEL = "Open Beelden (Beeld en Geluid)"
LICENSE = "mostly CC BY-SA (skipped); public-domain and CC BY items used"
NOTE = "Portrait rights: people in Dutch footage can object to use of their image."
NEEDS_KEY = False
HERO_OK = True
MIN_HEIGHT = 360

OAI = "https://www.openbeelden.nl/feeds/oai/"
PAGES_PER_SEARCH_PAGE = 5  # feed pages read for one page of search results


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse(xml: bytes | str) -> tuple[list[dict], str | None]:
    """Records (field -> list of values; media as (format, url)) and the resumption token."""
    root = ET.fromstring(xml)
    records, token = [], None
    for el in root.iter():
        name = _local(el.tag)
        if name == "resumptionToken":
            token = (el.text or "").strip() or None
        elif name == "record":
            rec: dict = {"media": []}
            for f in el.iter():
                fname = _local(f.tag)
                if fname == "medium":
                    rec["media"].append((f.get("format") or "", (f.text or "").strip()))
                elif f.text and f.text.strip() and fname not in ("record", "metadata", "header"):
                    rec.setdefault(fname, []).append(f.text.strip())
            records.append(rec)
    return records, token


def _words(query: str) -> list[str]:
    return [w for w in re.findall(r"[a-zà-ÿ0-9]+", query.lower()) if len(w) > 2
            and w not in ("subject", "and", "collection")]


def to_item(rec: dict) -> Item | None:
    ident = (rec.get("identifier") or [""])[0]
    if not ident:
        return None
    media = [url for fmt, url in rec["media"] if url and ("mp4" in fmt.lower() or url.lower().endswith(".mp4"))]
    media = media or [url for fmt, url in rec["media"] if url.lower().endswith((".webm", ".ogv", ".mpeg", ".mpg"))]
    pages = [v for v in rec.get("attributionURL", []) + rec.get("references", []) if v.startswith("http")]
    return Item(
        source=NAME, source_id=ident.rsplit(":", 1)[-1], title=strip_html((rec.get("title") or [""])[0]),
        description=strip_html(" ".join(rec.get("description", []) + rec.get("abstract", [])))[:5000],
        keywords=rec.get("subject", []), creator=strip_html((rec.get("attributionName") or rec.get("creator") or [""])[0]),
        source_url=pages[0] if pages else f"https://www.openbeelden.nl/media/{ident.rsplit(':', 1)[-1]}",
        date_created=(rec.get("date") or [None])[0], license_hint=(rec.get("license") or [""])[0],
        file_url=media[-1] if media else None,  # the last listed rendition is usually the largest
    )


def search(query: str, page: int = 1, session: requests.Session | None = None, key: str | None = None) -> list[Item]:
    s = session or requests.Session()
    tokens = s.__dict__.setdefault("_openbeelden_tokens", {})  # feed page -> resumption token
    words = _words(query)
    out = []
    first = (page - 1) * PAGES_PER_SEARCH_PAGE
    for feed_page in range(first, first + PAGES_PER_SEARCH_PAGE):
        if feed_page == 0:
            params = {"verb": "ListRecords", "metadataPrefix": "oai_oi"}
        elif feed_page in tokens:
            params = {"verb": "ListRecords", "resumptionToken": tokens[feed_page]}
        else:
            break
        r = s.get(OAI, params=params, headers=UA, timeout=180)  # ~12 s per feed page, sometimes far more
        r.raise_for_status()
        records, token = parse(r.content)
        if token:
            tokens[feed_page + 1] = token
        for rec in records:
            item = to_item(rec)
            if not item:
                continue
            text = f"{item.title} {' '.join(item.keywords)} {item.description}".lower()
            if not words or any(w in text for w in words):
                out.append(item)
        if not token:
            break
    return out


def rights(item: Item) -> Rights:
    r = license_from_url(item.license_hint)
    if not r.reject and not item.file_url:
        r.reject = "no video file in the feed"
    return cc_by_attribution(r, item.creator, "Open Beelden")


def file_url(item: Item, session: requests.Session | None = None, key: str | None = None) -> str | None:
    return item.file_url
