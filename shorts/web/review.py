"""Review: watch each Short, see every fact with its source, approve or reject. Filter per channel."""
from __future__ import annotations

import json

from flask import Blueprint, abort, redirect, render_template, request, send_file, url_for

from .. import db
from .common import channels, conn

bp = Blueprint("review", __name__)

REJECT_REASONS = ["weak hook", "wrong fact", "bad footage", "text too long", "boring topic",
                  "footage does not match text", "rights doubt", "other"]


def _next_id(status: str, after: int, channel: str | None) -> int | None:
    sql = "SELECT id FROM shorts WHERE status=? AND id>?"
    args: list = [status, after]
    if channel:
        sql += " AND channel=?"
        args.append(channel)
    row = conn().execute(sql + " ORDER BY id LIMIT 1", args).fetchone()
    return row[0] if row else None


@bp.get("/review")
def index():
    c = conn()
    status = request.args.get("status", "rendered")
    channel = request.args.get("channel") or None
    where, args = "s.status = ?", [status]
    if channel:
        where += " AND s.channel = ?"
        args.append(channel)
    shorts = c.execute(
        f"""SELECT s.*, (SELECT COUNT(*) FROM facts f WHERE f.short_id=s.id AND f.verdict!='supported') AS flagged
              FROM shorts s WHERE {where} ORDER BY s.id""", args).fetchall()
    count_sql = "SELECT status, COUNT(*) FROM shorts" + (" WHERE channel=?" if channel else "") + " GROUP BY status"
    counts = dict(c.execute(count_sql, [channel] if channel else []).fetchall())
    return render_template("review.html", shorts=shorts, status=status, counts=counts,
                           channel=channel, channels=channels())


@bp.get("/short/<int:sid>")
def short(sid: int):
    c = conn()
    s = c.execute("SELECT * FROM shorts WHERE id=?", (sid,)).fetchone()
    if not s:
        abort(404)
    blocks = c.execute("SELECT * FROM blocks WHERE short_id=? ORDER BY idx", (sid,)).fetchall()
    facts = c.execute("SELECT * FROM facts WHERE short_id=? ORDER BY verdict='supported', id", (sid,)).fetchall()
    assets = c.execute(
        """SELECT DISTINCT a.* FROM blocks b JOIN scenes sc ON sc.id=b.scene_id
             JOIN assets a ON a.id=sc.asset_id WHERE b.short_id=?""", (sid,)).fetchall()
    channel = request.args.get("channel") or None
    return render_template("short.html", s=s, blocks=blocks, facts=facts, assets=assets,
                           tags=", ".join(json.loads(s["tags"] or "[]")), reasons=REJECT_REASONS,
                           next_id=_next_id(s["status"], sid, channel), channel=channel)


@bp.get("/video/<int:sid>")
def video(sid: int):
    s = conn().execute("SELECT video_path FROM shorts WHERE id=?", (sid,)).fetchone()
    if not s or not s["video_path"]:
        abort(404)
    return send_file(s["video_path"], mimetype="video/mp4", conditional=True)


@bp.post("/short/<int:sid>")
def decide(sid: int):
    c = conn()
    action = request.form.get("action")
    channel = request.args.get("channel") or None
    title = (request.form.get("title") or "").strip()[:100]
    tags = [t.strip() for t in (request.form.get("tags") or "").split(",") if t.strip()]
    fields = {"title": title, "tags": json.dumps(tags)}
    if action == "approve":
        fields["status"] = "approved"
    elif action == "reject":
        reason = request.form.get("reason") or "other"
        note = (request.form.get("note") or "").strip()
        fields.update(status="rejected", reject_reason=f"{reason}: {note}" if note else reason)
    db.set_short(c, sid, **fields)
    if action == "save":
        return redirect(url_for("review.short", sid=sid, channel=channel))
    nxt = _next_id("rendered", sid, channel)
    return redirect(url_for("review.short", sid=nxt, channel=channel) if nxt
                    else url_for("review.index", channel=channel))
