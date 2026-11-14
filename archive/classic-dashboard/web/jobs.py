"""Make page and background jobs: start, follow progress, stop, finish or run again."""
from __future__ import annotations

import json

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from .. import jobs
from .common import channel_or_404, channels, conn, runner

bp = Blueprint("jobs", __name__)


def start_job(channel: str | None, args: list[str], label: str | None = None):
    job_id = jobs.submit(conn(), channel, args, label)
    return redirect(url_for("jobs.job", job_id=job_id))


@bp.get("/make")
def make_page():
    recent = conn().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 10").fetchall()
    return render_template("make.html", channels=channels(), selected=request.args.get("channel"),
                           recent=recent)


@bp.post("/make")
def make_start():
    ch = channel_or_404(request.form.get("channel"))
    count = max(1, min(50, int(request.form.get("count") or 5)))
    args = ["make", "-n", str(count)]
    fmt = request.form.get("format") or ""
    if fmt:
        if fmt not in {f["id"] for f in ch.formats}:
            abort(400, f"format {fmt} does not belong to channel {ch.id}")
        args += ["--format", fmt]
    return start_job(ch.id, args, f"Make {count} Shorts")


@bp.post("/jobs/start")
def start_simple():
    """Buttons elsewhere start a fixed command for a channel (e.g. 'upload')."""
    allowed = {"upload": "Schedule approved Shorts", "finish": "Finish Shorts that stopped halfway",
               "stats": "Fetch stats"}
    cmd = request.form.get("command")
    if cmd not in allowed:
        abort(400)
    ch = channel_or_404(request.form.get("channel"))
    return start_job(ch.id, [cmd], allowed[cmd])


@bp.get("/jobs")
def job_list():
    rows = conn().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("jobs.html", jobs=rows)


def _job_or_404(job_id: int):
    job = conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        abort(404)
    return job


@bp.get("/jobs/<int:job_id>")
def job(job_id: int):
    j = _job_or_404(job_id)
    return render_template("job.html", job=j, log=jobs.tail(j["log_path"]), follow_up=jobs.follow_up(j))


@bp.get("/jobs/<int:job_id>.json")
def job_json(job_id: int):
    j = _job_or_404(job_id)
    return jsonify({k: j[k] for k in ("id", "status", "progress", "progress_done", "progress_total",
                                      "exit_code", "started_at", "finished_at")}
                   | {"log": jobs.tail(j["log_path"])})


@bp.post("/jobs/<int:job_id>/stop")
def stop(job_id: int):
    _job_or_404(job_id)
    runner().stop(conn(), job_id)
    return redirect(url_for("jobs.job", job_id=job_id))


@bp.post("/jobs/<int:job_id>/again")
def again(job_id: int):
    j = _job_or_404(job_id)
    nxt = jobs.follow_up(j)
    if not nxt:
        abort(400)
    label, args = nxt
    return start_job(j["channel"], args, f"{label}: {j['label']}")


@bp.app_template_filter("args")
def args_filter(command: str) -> str:
    return " ".join(json.loads(command))
