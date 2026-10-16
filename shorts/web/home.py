"""Home: one card per channel."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template

from ..library import unused_hero_videos
from .common import channels, conn, settings

bp = Blueprint("home", __name__)


def channel_card(ch) -> dict:
    c = conn()
    counts = dict(c.execute("SELECT status, COUNT(*) FROM shorts WHERE channel=? GROUP BY status",
                            (ch.id,)).fetchall())
    now = datetime.now(timezone.utc)
    week = c.execute("""SELECT COUNT(*) FROM shorts WHERE channel=? AND status='scheduled'
                          AND publish_at >= ? AND publish_at < ?""",
                     (ch.id, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()[0]
    last_job = c.execute("SELECT * FROM jobs WHERE channel=? ORDER BY id DESC LIMIT 1", (ch.id,)).fetchone()
    return {
        "ch": ch,
        "connected": (settings().secrets_dir / f"token-{ch.id}.json").exists(),
        "review": counts.get("rendered", 0),
        "approved": counts.get("approved", 0),
        "failed": counts.get("failed", 0),
        "scheduled_week": week,
        "footage_left": unused_hero_videos(c, settings(), ch),
        "categories": [(cat, unused_hero_videos(c, settings(), ch, cat)) for cat in ch.categories],
        "last_job": last_job,
    }


@bp.get("/")
def index():
    running = conn().execute("SELECT * FROM jobs WHERE status IN ('running','queued') ORDER BY id").fetchall()
    return render_template("home.html", cards=[channel_card(ch) for ch in channels()], running=running)
