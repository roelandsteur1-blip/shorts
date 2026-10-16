"""Library: footage per category, fix wrong categories with one click, sort the uncategorised."""
from __future__ import annotations

import re

from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file, url_for

from .. import categorize, llm
from ..library import unused_hero_videos
from .common import channels, conn, settings
from .jobs import start_job

bp = Blueprint("library", __name__)
PAGE = 60


def _counts() -> dict[str, int]:
    return dict(conn().execute(
        """SELECT c.category, COUNT(*) FROM scene_categories c JOIN scenes sc ON sc.id = c.scene_id
            WHERE c.confidence > 0 AND sc.rejected = 0 GROUP BY c.category""").fetchall())


UNCAT_SQL = """FROM scenes sc JOIN assets a ON a.id = sc.asset_id
               WHERE sc.rejected = 0 AND sc.sorted = 1 AND sc.id NOT IN
                     (SELECT scene_id FROM scene_categories WHERE confidence > 0)"""


@bp.get("/library")
def index():
    c = conn()
    cats = categorize.load_categories(settings())
    counts = _counts()
    left = {cat.id: [(ch, unused_hero_videos(c, settings(), ch, cat.id))
                     for ch in channels() if cat.id in ch.categories] for cat in cats}
    uncat = c.execute("SELECT COUNT(*) " + UNCAT_SQL).fetchone()[0]
    unsorted = c.execute("SELECT COUNT(*) FROM scenes WHERE rejected = 0 AND sorted = 0").fetchone()[0]
    methods = dict(c.execute("SELECT method, COUNT(*) FROM scene_categories WHERE confidence > 0 GROUP BY method"))
    return render_template("library.html", cats=cats, counts=counts, left=left, uncat=uncat,
                           unsorted=unsorted, methods=methods)


def _scene_rows(where: str, args: list, page: int):
    return conn().execute(
        f"""SELECT sc.id, sc.start, sc.end, a.title, a.source, a.source_url,
                   (SELECT GROUP_CONCAT(category || ':' || method, ', ') FROM scene_categories
                     WHERE scene_id = sc.id AND confidence > 0) AS cats
            {where} ORDER BY sc.asset_id, sc.id LIMIT ? OFFSET ?""", (*args, PAGE, page * PAGE)).fetchall()


@bp.get("/library/<cat>")
def category(cat: str):
    cats = categorize.load_categories(settings())
    page = max(0, int(request.args.get("page", 0)))
    if cat == "uncategorised":
        rows = _scene_rows(UNCAT_SQL, [], page)
        label = "Uncategorised"
    else:
        found = next((x for x in cats if x.id == cat), None)
        if not found:
            abort(404)
        label = found.label
        rows = _scene_rows("""FROM scenes sc JOIN assets a ON a.id = sc.asset_id
                               WHERE sc.rejected = 0 AND sc.id IN (SELECT scene_id FROM scene_categories
                                     WHERE category = ? AND confidence > 0)""", [cat], page)
    return render_template("library_cat.html", cat=cat, label=label, rows=rows, cats=cats, page=page,
                           more=len(rows) == PAGE)


@bp.get("/keyframe/<int:scene_id>")
def keyframe(scene_id: int):
    r = conn().execute("SELECT keyframe FROM scenes WHERE id=?", (scene_id,)).fetchone()
    if not r or not r["keyframe"]:
        abort(404)
    return send_file(r["keyframe"], mimetype="image/jpeg", max_age=3600)


@bp.post("/scene/<int:scene_id>/category")
def set_category(scene_id: int):
    """move: this scene belongs in <category> (and not where it was); add: also <category>;
    remove: not <category>."""
    cat = request.form.get("category") or ""
    known = {c.id for c in categorize.load_categories(settings())}
    if cat not in known:
        abort(400)
    action = request.form.get("action", "move")
    if action == "move":
        categorize.move_scene(conn(), scene_id, cat)
    else:
        categorize.set_manual(conn(), scene_id, cat, on=action == "add")
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(request.referrer or url_for("library.index"))


@bp.post("/library/sort")
def sort():
    args = ["categorize"] + (["--all"] if request.form.get("all") else [])
    return start_job(None, args, "Re-sort all footage" if len(args) > 1 else "Sort new footage")


@bp.post("/library/draft")
def draft():
    name = (request.form.get("name") or "").strip()
    if not name:
        abort(400)
    try:
        return jsonify(categorize.draft_category(settings(), name))
    except llm.LLMError as e:
        return jsonify(error=str(e)), 502


def _lines(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[\n,]", text or "") if x.strip()]


@bp.post("/library/categories")
def add_category():
    f = request.form
    cid = (f.get("id") or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", cid):
        abort(400, "id: lowercase letters, digits and _ only")
    cats = categorize.load_categories(settings())
    search = {}
    for line in (f.get("search") or "").splitlines():
        if "=" in line:
            src, q = line.split("=", 1)
            if src.strip() and q.strip():
                search[src.strip()] = q.strip()
    new = categorize.Category(id=cid, label=(f.get("label") or cid).strip(),
                              keywords=[k.lower() for k in _lines(f.get("keywords"))],
                              clip_prompts=[p.strip() for p in (f.get("clip_prompts") or "").splitlines() if p.strip()],
                              search=search)
    cats = [c for c in cats if c.id != cid] + [new]
    categorize.save_categories(settings(), cats)
    return redirect(url_for("library.index"))
