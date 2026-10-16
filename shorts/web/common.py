"""Shared helpers for the dashboard pages."""
from __future__ import annotations

import sqlite3

from flask import abort, current_app, g

from .. import db
from ..config import Channel, Settings, list_channels, load_channel


def settings() -> Settings:
    return current_app.config["SETTINGS"]


def conn() -> sqlite3.Connection:
    if "conn" not in g:
        g.conn = db.connect(settings().db_path)
    return g.conn


def runner():
    return current_app.extensions["runner"]


def channels() -> list[Channel]:
    out = []
    for cid in list_channels(settings().config_dir):
        try:
            out.append(load_channel(cid, settings().config_dir))
        except Exception:  # noqa: BLE001 - a broken file must not take the dashboard down
            current_app.logger.exception("channel %s", cid)
    return out


def channel_or_404(cid: str | None) -> Channel:
    for ch in channels():
        if ch.id == cid:
            return ch
    abort(404)
