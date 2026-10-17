"""Voices page: listen to every voice and pick each channel's voice and speed in one place."""
from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, send_file, url_for

from .. import categorize, channels as chans, voice
from .common import channel_or_404, channels, conn, settings

bp = Blueprint("voices", __name__)


def _newest_text(cid: str) -> str | None:
    """The channel's newest script, so a channel can be heard reading its own kind of Short."""
    s = conn().execute("""SELECT id FROM shorts WHERE channel=? AND status != 'rejected'
                          ORDER BY id DESC LIMIT 1""", (cid,)).fetchone()
    if not s:
        return None
    return " ".join(r[0] for r in conn().execute(
        "SELECT text FROM blocks WHERE short_id=? ORDER BY idx", (s["id"],)))


@bp.get("/voices")
def index():
    uses = dict(conn().execute("""SELECT voice, COUNT(*) FROM shorts WHERE voice IS NOT NULL
                                  AND status != 'rejected' GROUP BY voice""").fetchall())
    chs = channels()
    return render_template("voices.html", voices=voice.VOICES, label=voice.label, speeds=voice.SPEEDS,
                           channels=chs, uses=uses, sample_text=voice.SAMPLE_TEXT,
                           voiced=sum(1 for c in chs if voice.channel_voice(c)),
                           engine_ok=voice.available(), saved=request.args.get("saved"))


@bp.get("/voices/sample/<v>")
def sample(v: str):
    if v not in voice.VOICES:
        abort(404)
    try:
        speed = float(request.args.get("speed") or 1.0)
    except ValueError:
        abort(400)
    text = _newest_text(channel_or_404(request.args["channel"]).id) if request.args.get("channel") else None
    try:
        path = voice.sample(settings(), v, speed, text)
    except RuntimeError as e:
        return str(e), 503
    return send_file(path, mimetype="audio/wav", conditional=True)


@bp.post("/voices/<cid>")
def save(cid: str):
    channel_or_404(cid)
    data = chans.raw(settings(), cid)
    data["voice"] = (request.form.get("voice") or "").strip() or None
    try:
        data["voice_speed"] = float(request.form.get("voice_speed") or 1.0)
    except ValueError:
        abort(400)
    if data["voice"] and data["voice"] not in voice.VOICES:
        abort(400)
    errors = chans.save(settings(), data, {c.id for c in categorize.load_categories(settings())})
    if errors:
        abort(400, "; ".join(errors))
    return redirect(url_for("voices.index", saved=cid))
