"""Local review page: watch each Short, see every fact with its source, approve or reject."""
from __future__ import annotations

import json
import sqlite3

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from . import db
from .config import Settings

REJECT_REASONS = ["weak hook", "wrong fact", "bad footage", "text too long", "boring topic",
                  "footage does not match text", "rights doubt", "other"]


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__)

    def conn() -> sqlite3.Connection:
        return db.connect(settings.db_path)

    @app.get("/")
    def index():
        c = conn()
        status = request.args.get("status", "rendered")
        shorts = c.execute(
            """SELECT s.*, (SELECT COUNT(*) FROM facts f WHERE f.short_id=s.id AND f.verdict!='supported') AS flagged
                 FROM shorts s WHERE s.status = ? ORDER BY s.id""", (status,)).fetchall()
        counts = dict(c.execute("SELECT status, COUNT(*) FROM shorts GROUP BY status").fetchall())
        return render_template("index.html", shorts=shorts, status=status, counts=counts)

    @app.get("/short/<int:sid>")
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
        nxt = c.execute("SELECT id FROM shorts WHERE status=? AND id>? ORDER BY id LIMIT 1",
                        (s["status"], sid)).fetchone()
        return render_template("short.html", s=s, blocks=blocks, facts=facts, assets=assets,
                               tags=", ".join(json.loads(s["tags"] or "[]")), reasons=REJECT_REASONS,
                               next_id=nxt[0] if nxt else None)

    @app.get("/video/<int:sid>")
    def video(sid: int):
        s = conn().execute("SELECT video_path FROM shorts WHERE id=?", (sid,)).fetchone()
        if not s or not s["video_path"]:
            abort(404)
        return send_file(s["video_path"], mimetype="video/mp4", conditional=True)

    @app.post("/short/<int:sid>")
    def decide(sid: int):
        c = conn()
        action = request.form.get("action")
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
            return redirect(url_for("short", sid=sid))
        nxt = c.execute("SELECT id FROM shorts WHERE status='rendered' AND id>? ORDER BY id LIMIT 1",
                        (sid,)).fetchone()
        return redirect(url_for("short", sid=nxt[0]) if nxt else url_for("index"))

    return app


def serve(settings: Settings, port: int = 5000) -> None:
    app = create_app(settings)
    print(f"Review page: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
