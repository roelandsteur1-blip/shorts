"""Library: which videos each channel uses (fix with one click), and footage per category."""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file, url_for

from .. import categorize, embed, fit
from ..library import shorts_left
from .common import channels, conn, settings
from .jobs import start_job

bp = Blueprint("library", __name__)
PAGE = 60


def _counts() -> dict[str, int]:
    return dict(conn().execute(
        """SELECT c.category, COUNT(*) FROM scene_categories c JOIN scenes sc ON sc.id = c.scene_id
            WHERE c.confidence > 0 AND sc.rejected = 0 GROUP BY c.category""").fetchall())


@bp.get("/library")
def index():
    c = conn()
    cats = categorize.load_categories(settings())
    counts = _counts()
    left = {cat.id: [(ch, shorts_left(c, settings(), ch, cat.id))
                     for ch in channels() if cat.id in ch.categories] for cat in cats}
    unsorted = c.execute("SELECT COUNT(*) FROM scenes WHERE rejected = 0 AND sorted = 0").fetchone()[0]
    methods = dict(c.execute("SELECT method, COUNT(*) FROM scene_categories WHERE confidence > 0 GROUP BY method"))
    chans = channels()
    waiting = fit.pending(c, chans)
    per_channel = []
    for ch in chans:
        yes, no = (c.execute(f"""SELECT COUNT(*) FROM channel_fit f JOIN assets a ON a.id = f.asset_id
                                WHERE f.channel=? AND f.fits=? AND f.niche IN (?, '{fit.ANY_NICHE}') AND {fit.USABLE}""",
                             (ch.id, v, ch.niche)).fetchone()[0] for v in (1, 0))
        per_channel.append({"ch": ch, "fits": yes, "no": no, "left": shorts_left(c, settings(), ch),
                            "waiting": sum(ch in v for v in waiting.values())})
    return render_template("library.html", cats=cats, counts=counts, left=left,
                           unsorted=unsorted, methods=methods, per_channel=per_channel,
                           pe=embed.pe_status(c), space=embed.search_space(c, settings()), pe_ready=embed.PE_READY)


@bp.get("/library/channel/<cid>")
def channel_videos(cid: str):
    """Videos sorted for one channel: the ones it uses, or (show=no) the ones it doesn't."""
    ch = next((x for x in channels() if x.id == cid), None)
    if not ch:
        abort(404)
    show = 0 if request.args.get("show") == "no" else 1
    page = max(0, int(request.args.get("page", 0)))
    rows = conn().execute(
        f"""SELECT a.id, a.title, a.source, a.source_url, f.reason, f.method,
                   (SELECT MIN(s.id) FROM scenes s WHERE s.asset_id = a.id AND s.rejected = 0) AS scene
              FROM channel_fit f JOIN assets a ON a.id = f.asset_id
             WHERE f.channel=? AND f.fits=? AND f.niche IN (?, '{fit.ANY_NICHE}') AND {fit.USABLE}
             ORDER BY f.method = 'manual' DESC, a.id DESC LIMIT ? OFFSET ?""",
        (ch.id, show, ch.niche, PAGE, page * PAGE)).fetchall()
    return render_template("library_channel.html", ch=ch, rows=rows, show=show, page=page,
                           more=len(rows) == PAGE)


@bp.post("/library/channel/<cid>/<int:asset_id>")
def set_fit(cid: str, asset_id: int):
    """By hand: this video is (fits=1) or isn't (fits=0) for this channel. Sticks for good."""
    ch = next((x for x in channels() if x.id == cid), None)
    if not ch:
        abort(404)
    fit.mark(conn(), ch, asset_id, request.form.get("fits") == "1", "set by hand")
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(request.referrer or url_for("library.channel_videos", cid=cid))


@bp.get("/keyframe/<int:scene_id>")
def keyframe(scene_id: int):
    r = conn().execute("SELECT keyframe FROM scenes WHERE id=?", (scene_id,)).fetchone()
    if not r or not r["keyframe"]:
        abort(404)
    return send_file(r["keyframe"], mimetype="image/jpeg", max_age=3600)


@bp.post("/library/sort")
def sort():
    args = ["categorize"] + (["--all"] if request.form.get("all") else [])
    return start_job(None, args, "Re-sort all footage" if len(args) > 1 else "Sort new footage")
