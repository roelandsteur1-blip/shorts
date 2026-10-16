"""Coverr: off. Its license explicitly bans AI use of the videos, and our footage search runs every
scene through CLIP. Kept in the list so the Sources page says why; not used until that's cleared."""
from __future__ import annotations

from . import Item, Rights

NAME = "coverr"
LABEL = "Coverr"
LICENSE = "Coverr License: bans AI use of the videos (skipped until CLIP search is cleared)"
NEEDS_KEY = True
KEY_URL = "https://coverr.co/developers"
HERO_OK = False


def search(query: str, page: int = 1, session=None, key: str | None = None) -> list[Item]:
    raise ValueError("Coverr is not used: its license bans AI use of the videos (our search uses CLIP)")


def rights(item: Item) -> Rights:
    return Rights(reject="Coverr license bans AI use")


def file_url(item: Item, session=None, key=None) -> str | None:
    return None
