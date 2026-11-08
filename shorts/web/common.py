"""Shared helpers for the dashboard pages."""
from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

from flask import abort, current_app, g, has_request_context, request

from .. import db
from ..config import Channel, Settings, all_channels


def settings() -> Settings:
    return current_app.config["SETTINGS"]


def conn() -> sqlite3.Connection:
    if "conn" not in g:
        g.conn = db.connect(settings().db_path)
    return g.conn


def runner():
    return current_app.extensions["runner"]


def channels() -> list[Channel]:
    return all_channels(settings().config_dir)


def channel_or_404(cid: str | None) -> Channel:
    for ch in channels():
        if ch.id == cid:
            return ch
    abort(404)


# ---- Filter chips: show only channels of some video types and/or topics (Roeland, 8 Nov 2026).
# The pick is kept in a cookie, so it stays on while moving between pages; `f=1` in the URL marks a
# new pick (also an empty one: "All").
FILTER_COOKIE = "chfilter"
OTHER_TOPIC = "other"


def channel_type(ch: Channel) -> str:
    from ..metatype import TYPES
    return ch.video_type if ch.video_type in TYPES else "classic"


def channel_topic(ch: Channel) -> str:
    from ..metatype import niche_for
    return niche_for(ch) or OTHER_TOPIC


def _parse(raw: str) -> dict[str, list[str]]:
    out = {"type": [], "topic": []}
    for part in (raw or "").split("|"):
        key, _, vals = part.partition(":")
        if key in out:
            out[key] = [v for v in vals.split(",") if v]
    return out


def chip_filter() -> dict[str, list[str]]:
    """The picked types and topics (empty list = all) from the URL, else from the cookie."""
    if not has_request_context():  # called from code or a test outside a page: no filter
        return {"type": [], "topic": []}
    if "f" in request.args:
        return {"type": [v for v in request.args.getlist("type") if v],
                "topic": [v for v in request.args.getlist("topic") if v]}
    return _parse(request.cookies.get(FILTER_COOKIE, ""))


def filter_cookie_value(f: dict[str, list[str]]) -> str:
    return "|".join(f"{k}:{','.join(v)}" for k, v in f.items() if v)


def matches(ch: Channel, f: dict[str, list[str]]) -> bool:
    return ((not f["type"] or channel_type(ch) in f["type"])
            and (not f["topic"] or channel_topic(ch) in f["topic"]))


def filter_active() -> bool:
    f = chip_filter()
    return bool(f["type"] or f["topic"])


def shown_channels() -> list[Channel]:
    """The channels the filter chips let through (all when nothing is picked)."""
    f = chip_filter()
    return [ch for ch in channels() if matches(ch, f)]


def scope(channel: str | None = None) -> list[str] | None:
    """Channel ids a page shows: the one asked for, else the filtered ones; None = every channel."""
    if channel:
        return [channel]
    if not filter_active():
        return None
    return [ch.id for ch in shown_channels()]


def in_scope(col: str, ids: list[str] | None) -> tuple[str, list]:
    """SQL ' AND col IN (...)' for a scope (nothing when every channel is shown)."""
    if ids is None:
        return "", []
    if not ids:
        return " AND 0", []
    return f" AND {col} IN ({','.join('?' * len(ids))})", list(ids)


def chips() -> dict:
    """What the chip bar shows: each type and topic with its number of channels and whether it is on."""
    from ..metatype import NICHE_LABELS, TYPES
    f = chip_filter()
    chs = channels()
    types = [{"key": k, "label": v.split(":")[0], "n": sum(channel_type(c) == k for c in chs), "on": k in f["type"]}
             for k, v in TYPES.items()]
    topic_labels = dict(NICHE_LABELS, **{OTHER_TOPIC: "Other"})
    topics = [{"key": k, "label": v, "n": sum(channel_topic(c) == k for c in chs), "on": k in f["topic"]}
              for k, v in topic_labels.items()]
    shown = sum(matches(c, f) for c in chs)
    return {"types": [t for t in types if t["n"]], "topics": [t for t in topics if t["n"]],
            "active": bool(f["type"] or f["topic"]), "shown": shown, "total": len(chs), "f": f}


def chip_url(f: dict[str, list[str]], key: str | None = None, value: str | None = None) -> str:
    """This page's URL with one chip switched (or every chip off when key is None)."""
    picked = {k: list(v) for k, v in f.items()} if key else {"type": [], "topic": []}
    if key:
        vals = picked[key]
        vals.remove(value) if value in vals else vals.append(value)
    args = [(k, v) for k, v in request.args.items(multi=True) if k not in ("f", "type", "topic", "channel")]
    args.append(("f", "1"))
    args += [(k, v) for k in ("type", "topic") for v in picked[k]]
    return request.path + "?" + urlencode(args)


def picker_channels(selected: str | None = None) -> list[Channel]:
    """The channel drop-down: the filtered channels, plus the one picked when the filter hides it."""
    shown = shown_channels()
    if selected and all(ch.id != selected for ch in shown):
        shown += [ch for ch in channels() if ch.id == selected]
    return shown
