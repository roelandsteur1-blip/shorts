"""Bench page (/bench): progress and results of the clip quality bench (shorts/bench), and a short
check of Claude's labels by a person, one clip at a time, so the report can say how far the judge
can be trusted."""
from __future__ import annotations

import random

from flask import Blueprint, abort, redirect, render_template, request, url_for

from ..bench import SEED, load, root, save
from .common import settings

bp = Blueprint("bench", __name__)

SAMPLE = 60


def _sample() -> list[dict]:
    """The clips to check, picked once: as many judged on topic as related and as not."""
    r = root(settings())
    sample = load(r / "human-sample.json")
    if sample is not None:
        return sample
    labels = load(r / "judge.json", {})
    ts = load(r / "testset.json", {})
    if not labels or not ts:
        return []
    blocks = {str(b["id"]): b for b in ts["blocks"]}
    by_label: dict[int, list] = {0: [], 1: [], 2: []}
    for key, per in labels.items():
        for sid, (topic, _) in per.items():
            if key in blocks and topic in by_label:
                by_label[topic].append((key, sid))
    rng = random.Random(SEED)
    sample = []
    for topic in (2, 1, 0):
        pairs = sorted(by_label[topic])
        rng.shuffle(pairs)
        for key, sid in pairs[:SAMPLE // 3]:
            b = blocks[key]
            sample.append({"block": key, "sid": sid, "text": b["text"], "visual": b["visual"], "topic": b["topic"]})
    rng.shuffle(sample)
    save(r / "human-sample.json", sample)
    return sample


@bp.get("/bench")
def index():
    r = root(settings())
    report = (r / "report.md").read_text(encoding="utf-8") if (r / "report.md").exists() else None
    human = load(r / "human.json", {})
    return render_template("bench.html", progress=load(r / "progress.json", {}), report=report,
                           done=len(human), total=len(_sample()))


@bp.get("/bench/check")
def check():
    sample = _sample()
    human = load(root(settings()) / "human.json", {})
    todo = [s for s in sample if f"{s['block']}:{s['sid']}" not in human]
    if not todo:
        return redirect(url_for("bench.index"))
    return render_template("bench_check.html", item=todo[0], done=len(sample) - len(todo), total=len(sample))


@bp.post("/bench/check")
def save_check():
    key = f"{request.form['block']}:{request.form['sid']}"
    topic, watch = request.form.get("topic"), request.form.get("watch")
    if topic not in ("0", "1", "2") or watch not in ("0", "1"):
        abort(400)
    path = root(settings()) / "human.json"
    human = load(path, {})
    human[key] = [int(topic), int(watch)]
    save(path, human)
    return redirect(url_for("bench.check"))


@bp.get("/bench/strip/<int:sid>.jpg")
def strip_image(sid: int):
    from ..bench.frames import load_frames
    from ..bench.judge import jpeg, strip
    frames = load_frames(settings(), sid)
    if not any(f is not None for f in frames):
        abort(404)
    return jpeg(strip(frames, "")), 200, {"Content-Type": "image/jpeg", "Cache-Control": "max-age=86400"}
