"""Shared helpers for the dashboard pages."""
from __future__ import annotations

import sqlite3

from flask import abort, current_app, g

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
