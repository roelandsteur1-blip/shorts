"""Sources page: every footage source with on/off, API key, license and what it delivered."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait

from flask import Blueprint, abort, redirect, render_template, request, url_for

from .. import sources
from .common import conn, settings
from .jobs import start_job

bp = Blueprint("sources", __name__)
TEST_WAIT = 45  # seconds the page waits for a test; a slower archive keeps going and saves its result later
_pool = ThreadPoolExecutor(max_workers=8)


@bp.get("/sources")
def index():
    c = conn()
    rows = []
    off = sources.disabled(settings())
    tests = sources.test_results(settings())
    for mod in sources.all_sources():
        counts = dict(c.execute("SELECT status, COUNT(*) FROM assets WHERE source=? GROUP BY status",
                                (mod.NAME,)).fetchall())
        reasons = c.execute("""SELECT error, COUNT(*) AS n FROM assets WHERE source=? AND status='rejected'
                                GROUP BY error ORDER BY n DESC LIMIT 3""", (mod.NAME,)).fetchall()
        ok, why = sources.usable(settings(), mod.NAME)
        state, state_msg = sources.status(settings(), mod.NAME)
        rows.append({"mod": mod, "state": state, "state_msg": state_msg,
                     "tested_at": (tests.get(mod.NAME) or {}).get("at", "")[:16].replace("T", " "), "on": mod.NAME not in off, "usable": ok, "why": why,
                     "has_key": bool(sources.api_key(settings(), mod.NAME)),
                     "downloaded": sum(v for k, v in counts.items() if k in ("downloaded", "scened", "cleaned")),
                     "rejected": counts.get("rejected", 0), "failed": counts.get("failed", 0),
                     "reasons": reasons})
    summary = {k: sum(r["state"] == k for r in rows) for k in ("connected", "failing", "needs-key", "untested", "off")}
    return render_template("sources.html", rows=rows, summary=summary, total=len(rows))


def _run_tests(names: list[str]) -> None:
    from ..sources.common import test_connection
    futures = [_pool.submit(test_connection, settings(), n) for n in names]
    wait(futures, timeout=TEST_WAIT)


@bp.post("/sources/<name>")
def update(name: str):
    try:
        mod = sources.get(name)
    except KeyError:
        abort(404)
    if "enabled" in request.form:
        sources.set_enabled(settings(), name, request.form["enabled"] == "1")
    if "api_key" in request.form and getattr(mod, "NEEDS_KEY", False):
        key = request.form["api_key"].strip()
        sources.set_api_key(settings(), name, key)
        if key and name not in sources.disabled(settings()):
            _run_tests([name])  # a new key is tested straight away
    return redirect(url_for("sources.index") + f"#src-{name}")


@bp.post("/sources/<name>/test")
def test(name: str):
    try:
        sources.get(name)
    except KeyError:
        abort(404)
    _run_tests([name])
    return redirect(url_for("sources.index") + f"#src-{name}")


@bp.post("/sources-test-all")
def test_all():
    names = [m.NAME for m in sources.all_sources() if sources.status(settings(), m.NAME)[0]
             in ("connected", "failing", "untested")]
    _run_tests(names)
    return redirect(url_for("sources.index"))


@bp.post("/sources/<name>/try")
def try_search(name: str):
    query = (request.form.get("query") or "").strip()
    if not query:
        abort(400)
    return start_job(None, ["sources", "--try", name, query], f"Try {name}: {query}")

