"""Prelinger Archives (on the Internet Archive): ~65% public domain in the US.

Same API as internet_archive.py, limited to the 'prelinger' collection. Each item's license field
is checked; people in frame may have portrait rights, so faces in close-up deserve a look in review.
"""
from __future__ import annotations

import requests

from . import Item, Rights, internet_archive

NAME = "prelinger"
LABEL = "Prelinger Archives"
LICENSE = "mostly public domain in the US; each item's license field is checked"
NEEDS_KEY = False
HERO_OK = True
MIN_HEIGHT = 360
BASE_FILTER = "mediatype:(movies) AND collection:(prelinger)"


def search(query: str, page: int = 1, session: requests.Session | None = None, key: str | None = None) -> list[Item]:
    return internet_archive.search(query, page, session, key, base=BASE_FILTER, source=NAME)


def rights(item: Item) -> Rights:
    return internet_archive.rights(item)


file_url = internet_archive.file_url
