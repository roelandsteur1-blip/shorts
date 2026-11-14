"""Music page: per channel the tracks it uses, their mood, how often they're used, and the
'claimed by YouTube' button. Fetching runs as a job ('python -m shorts music')."""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, send_file, url_for

from .. import music, sources
from .common import channel_or_404, channels, conn, settings
from .jobs import start_job

bp = Blueprint("music", __name__)


@bp.get("/music")
def index():
    chans = channels()
    if not chans:
        abort(404)
    ch = channel_or_404(request.args.get("channel") or chans[0].id)
    c = conn()
    music.sync(c, settings())
    uses = dict(c.execute("""SELECT music, COUNT(*) FROM shorts WHERE music IS NOT NULL
                              AND status != 'rejected' GROUP BY music""").fetchall())
    tracks = c.execute("""SELECT * FROM tracks WHERE status IN ('ok', 'claimed')
                           ORDER BY status DESC, mood, id""").fetchall()
    rejected = c.execute("""SELECT note, COUNT(*) AS n FROM tracks WHERE status='rejected'
                             GROUP BY note ORDER BY n DESC LIMIT 5""").fetchall()
    return render_template("music.html", channels=chans, ch=ch, tracks=tracks, uses=uses,
                           scores=music.track_scores(c),
                           summary=music.summary(c), folder=music.folder(settings()),
                           moods=list(music.MOODS), rejected=rejected,
                           sources=music.music_sources(settings()),
                           freesound_key=bool(sources.api_key(settings(), "freesound")),
                           min_tracks=settings().music_min_tracks, terms=music.all_terms(settings()))


@bp.get("/music/file/<int:track_id>")
def file(track_id: int):
    t = conn().execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not t or not t["path"] or not Path(t["path"]).exists():
        abort(404)
    return send_file(t["path"], conditional=True)


@bp.post("/music/fetch")
def fetch():
    ch = channel_or_404(request.form.get("channel"))
    want = max(1, min(500, int(request.form.get("want") or settings().music_min_tracks or 30)))
    return start_job(ch.id, ["music", "--want", str(want)], f"Fetch music ({want} tracks)")


@bp.post("/music/<int:track_id>")
def update(track_id: int):
    ch = channel_or_404(request.form.get("channel"))
    action = request.form.get("action")
    if action == "mood":
        try:
            music.set_mood(conn(), track_id, request.form.get("mood", ""))
        except ValueError:
            abort(400)
    elif action == "remove":
        music.remove(conn(), track_id)
    elif action == "claimed":
        return start_job(ch.id, ["music", "--claimed", str(track_id)], f"Claimed track {track_id}: re-render")
    else:
        abort(400)
    return redirect(url_for("music.index", channel=ch.id))


@bp.post("/music/key")
def key():
    sources.set_api_key(settings(), "freesound", request.form.get("api_key", ""))
    return redirect(url_for("music.index", channel=request.form.get("channel")))
