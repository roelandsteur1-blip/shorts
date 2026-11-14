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
    if not labels or not ts or len(labels) < 0.9 * len(ts["blocks"]):
        return []  # not before the judge is (nearly) done: the sample must spread over all blocks
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
    from ..bench import chains
    return render_template("bench.html", progress=load(r / "progress.json", {}), report=report,
                           done=len(human), total=len(_sample()),
                           review_done=len(load(r / "review.json", {})), review_total=len(load(chains.path(settings()), {})))


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


@bp.get("/bench/review")
def review():
    """Step 4: the same moment three ways (shorts/bench/chains.py), under letters only."""
    from ..bench import chains
    data = load(chains.path(settings()), {})
    picks = load(root(settings()) / "review.json", {})
    todo = [k for k in data if k not in picks]
    if not todo:
        return redirect(url_for("bench.index"))
    key = todo[0]
    return render_template("bench_review.html", key=key, item=data[key], letters=sorted(chains.letters(key)),
                           done=len(data) - len(todo), total=len(data))


@bp.post("/bench/review")
def save_review():
    from ..bench import chains
    key, best = request.form.get("block", ""), request.form.get("best", "")
    order = chains.letters(key)
    if best != "same" and best not in order:
        abort(400)
    path = root(settings()) / "review.json"
    picks = load(path, {})
    picks[key] = order.get(best, "same")
    save(path, picks)
    return redirect(url_for("bench.review"))


@bp.get("/bench/clip/<int:block>/<letter>.mp4")
def review_clip(block: int, letter: str):
    """By letter, not by version name: the address must not give the answer away."""
    from flask import send_file

    from ..bench import chains
    chain = chains.letters(block).get(letter)
    f = chains.clip_file(settings(), block, chain) if chain else None
    if f is None or not f.exists():
        abort(404)
    return send_file(f, mimetype="video/mp4", conditional=True, max_age=86400)
