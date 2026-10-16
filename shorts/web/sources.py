"""Sources page: every footage source with on/off, API key, license and what it delivered."""
from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from .. import sources
from .common import conn, settings
from .jobs import start_job

bp = Blueprint("sources", __name__)


@bp.get("/sources")
def index():
    c = conn()
    rows = []
    off = sources.disabled(settings())
    for mod in sources.all_sources():
        counts = dict(c.execute("SELECT status, COUNT(*) FROM assets WHERE source=? GROUP BY status",
                                (mod.NAME,)).fetchall())
        reasons = c.execute("""SELECT error, COUNT(*) AS n FROM assets WHERE source=? AND status='rejected'
                                GROUP BY error ORDER BY n DESC LIMIT 3""", (mod.NAME,)).fetchall()
        ok, why = sources.usable(settings(), mod.NAME)
        rows.append({"mod": mod, "on": mod.NAME not in off, "usable": ok, "why": why,
                     "has_key": bool(sources.api_key(settings(), mod.NAME)),
                     "downloaded": sum(v for k, v in counts.items() if k in ("downloaded", "scened", "cleaned")),
                     "rejected": counts.get("rejected", 0), "failed": counts.get("failed", 0),
                     "reasons": reasons})
    return render_template("sources.html", rows=rows)


@bp.post("/sources/<name>")
def update(name: str):
    try:
        mod = sources.get(name)
    except KeyError:
        abort(404)
    if "enabled" in request.form:
        sources.set_enabled(settings(), name, request.form["enabled"] == "1")
    if "api_key" in request.form and getattr(mod, "NEEDS_KEY", False):
        sources.set_api_key(settings(), name, request.form["api_key"])
    return redirect(url_for("sources.index"))


@bp.post("/sources/<name>/try")
def try_search(name: str):
    query = (request.form.get("query") or "").strip()
    if not query:
        abort(400)
    return start_job(None, ["sources", "--try", name, query], f"Try {name}: {query}")
