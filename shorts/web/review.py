"""Review: watch each Short, see every fact with its source, approve or reject. Filter per channel."""
from __future__ import annotations

import json

from flask import Blueprint, abort, redirect, render_template, request, send_file, url_for

from .. import db, script
from .common import channel_or_404, channels, conn
from .jobs import start_job

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
    # Other Shorts opened by the same video (long videos give several), in order of making.
    family = c.execute(
        """SELECT s.id, s.status FROM shorts s JOIN scenes sc ON sc.id = s.hero_scene_id
            WHERE (s.status != 'rejected' OR s.id = ?) AND sc.asset_id =
                  (SELECT asset_id FROM scenes WHERE id = ?) ORDER BY s.id""", (sid, s["hero_scene_id"])).fetchall()
    channel = request.args.get("channel") or None
    return render_template("short.html", s=s, blocks=blocks, facts=facts, assets=assets, family=family,
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


@bp.post("/short/<int:sid>/claim")
def claim(sid: int):
    """Log a copyright claim: the ticked videos are never used in a Short again."""
    s = conn().execute("SELECT id FROM shorts WHERE id=?", (sid,)).fetchone()
    if not s:
        abort(404)
    ids = [int(x) for x in request.form.getlist("asset") if x.isdigit()]
    if ids:
        db.log_claim(conn(), sid, ids, (request.form.get("claimant") or "").strip()[:200],
                     (request.form.get("note") or "").strip()[:500])
    return redirect(url_for("review.short", sid=sid))


@bp.post("/short/<int:sid>/blocks")
def edit_blocks(sid: int):
    """Fix a text block and re-render, instead of rejecting the Short for one wrong word."""
    s = conn().execute("SELECT * FROM shorts WHERE id=?", (sid,)).fetchone()
    if not s:
        abort(404)
    if s["youtube_id"]:
        abort(400, "already uploaded: change it in YouTube Studio")
    texts = {int(k.removeprefix("block_")): v for k, v in request.form.items() if k.startswith("block_")}
    if not script.edit_blocks(conn(), channel_or_404(s["channel"]), sid, texts):
        return redirect(url_for("review.short", sid=sid))
    return start_job(s["channel"], ["rerender", str(sid)], f"Re-render #{sid} after text edit")
