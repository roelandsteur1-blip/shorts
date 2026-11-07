"""Cached numbers for the dashboard pages. Footage counts take seconds over 60,000+ scenes; they are
worked out again only when the data changed (hero.data_version) or after MAX_AGE, and while that
happens in a background thread the page shows the last answer, so no page waits for it."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from flask import g

from .. import db, embed, hero
from ..config import Channel
from .common import conn, settings

log = logging.getLogger(__name__)

MAX_AGE = 300  # seconds: worked out again after this even when nothing seems to have changed

_store: dict[tuple, tuple[float, tuple, Any]] = {}
_busy: set[tuple] = set()
_lock = threading.Lock()


def version() -> tuple:
    """hero.data_version, asked once per page."""
    if "data_version" not in g:
        g.data_version = hero.data_version(conn())
    return g.data_version


def get(key: tuple, compute: Callable, background: bool = True) -> Any:
    """compute(conn) cached under key. Out of date and background: the old value now, a fresh one
    worked out in a thread (its own database connection) for the next visit."""
    s, ver = settings(), version()
    key = (str(s.db_path), *key)
    hit = _store.get(key)
    if hit and hit[1] == ver and time.monotonic() - hit[0] < MAX_AGE:
        return hit[2]
    if hit and background:
        _refresh(key, compute, s)
        return hit[2]
    value = compute(conn())
    _store[key] = (time.monotonic(), ver, value)
    return value


def _refresh(key: tuple, compute: Callable, s) -> None:
    with _lock:
        if key in _busy:
            return
        _busy.add(key)

    def run():
        try:
            c = db.connect(s.db_path)
            try:
                ver = hero.data_version(c)
                _store[key] = (time.monotonic(), ver, compute(c))
            finally:
                c.close()
        except Exception:  # noqa: BLE001 - the old value stays; never take a page down
            log.exception("refreshing %s failed", key[1:])
        finally:
            with _lock:
                _busy.discard(key)

    threading.Thread(target=run, name="dashboard-cache", daemon=True).start()


def footage(ch: Channel) -> dict:
    """hero.footage_summary for a channel: {"left", "videos", "categories": {cat: left}}."""
    s = settings()
    key = ("footage", ch.id, ch.niche, tuple(ch.categories), s.segment_seconds, s.max_shorts_per_video)
    return get(key, lambda c: hero.footage_summary(c, s, ch))


def category_counts() -> dict[str, int]:
    """Usable scenes per category (Library and the channel page)."""
    return get(("categories",), lambda c: dict(c.execute(
        """SELECT c.category, COUNT(*) FROM scene_categories c JOIN scenes sc ON sc.id = c.scene_id
            WHERE c.confidence > 0 AND sc.rejected = 0 GROUP BY c.category""").fetchall()))


def search_counts() -> tuple:
    """embed._counts (Perception Encoder progress on the Library page)."""
    return get(("search",), embed._counts)


def wait_idle(timeout: float = 10.0) -> None:
    """Tests: until background refreshes are done."""
    end = time.monotonic() + timeout
    while _busy and time.monotonic() < end:
        time.sleep(0.02)
