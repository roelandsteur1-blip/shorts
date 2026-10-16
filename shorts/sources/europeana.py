"""Europeana: open-reuse videos from European archives (free API key: pro.europeana.eu).

Searched with reusability=open (public domain, CC0, CC BY, CC BY-SA); CC BY-SA is then rejected.
Many items only link out to the holding archive's own player: those are skipped too."""
from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlsplit

import requests

from . import Item, Rights, as_list, cc_by_attribution, license_from_url, strip_html
from .common import UA

NAME = "europeana"
LABEL = "Europeana"
LICENSE = "public domain, CC0, CC BY (credit needed); CC BY-SA skipped"
NOTE = "Many items only link to the holding archive's own player; only direct video files are used."
NEEDS_KEY = True
KEY_URL = "https://pro.europeana.eu/pages/get-api"
HERO_OK = True
MIN_HEIGHT = 360

API = "https://api.europeana.eu/record/v2/search.json"
PAGE = 50
VIDEO_EXT = (".mp4", ".m4v", ".webm", ".ogv", ".mov", ".mpg", ".mpeg")


def _first(v) -> str:
    v = as_list(v) if not isinstance(v, list) else v
    return str(v[0]) if v else ""


def search(query: str, page: int = 1, session: requests.Session | None = None, key: str | None = None) -> list[Item]:
    if not key:
        raise ValueError("Europeana needs an API key (Sources page)")
    s = session or requests.Session()
    r = s.get(API, params={"wskey": key, "query": query, "qf": "TYPE:VIDEO", "reusability": "open",
                           "media": "true", "rows": PAGE, "start": (page - 1) * PAGE + 1, "profile": "rich"},
              headers=UA, timeout=60)
    r.raise_for_status()
    items = []
    for d in r.json().get("items") or []:
        concepts = [c.get("def") if isinstance(c, dict) else c for c in d.get("edmConceptLabel") or []]
        shown_by = _first(d.get("edmIsShownBy"))
        items.append(Item(
            source=NAME, source_id=str(d.get("id", "")).strip("/"), title=strip_html(_first(d.get("title"))),
            description=strip_html(d.get("dcDescription"))[:5000],
            keywords=as_list(concepts) + as_list(d.get("dcSubject")),
            creator=strip_html(_first(d.get("dcCreator")) or _first(d.get("dataProvider"))),
            source_url=d.get("guid") or _first(d.get("edmIsShownAt")),
            date_created=_first(d.get("year")) or None, license_hint=_first(d.get("rights")),
            file_url=shown_by if PurePosixPath(urlsplit(shown_by).path.lower()).suffix in VIDEO_EXT else None,
            extra={"provider": _first(d.get("dataProvider"))},
        ))
    return [i for i in items if i.source_id]


def rights(item: Item) -> Rights:
    r = license_from_url(item.license_hint)
    if not r.reject and not item.file_url:
        r.reject = "no direct video file (only a link to the holding archive's player)"
    return cc_by_attribution(r, item.creator, "Europeana")


def file_url(item: Item, session: requests.Session | None = None, key: str | None = None) -> str | None:
    return item.file_url
