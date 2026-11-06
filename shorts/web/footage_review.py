"""Temporary review pages for the footage collector research (/bench/footage): round 2 (which
downloaded clips are bad) and round 2b (which crop is best), one item at a time, blind. Reads the
research job outputs in ~/meta-out and saves the marks to ~/meta-out/marks/footage.json, which the
runner's "marks" job copies into the results repo."""
from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, send_file, url_for

bp = Blueprint("footage_review", __name__)

OUT = Path.home() / "meta-out"


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def marks_path() -> Path:
    return OUT / "marks" / "footage.json"


def _marks() -> dict:
    m = _load(marks_path(), {})
    m.setdefault("clips", {})
    m.setdefault("crops", {})
    m.setdefault("shorts", {})
    return m


def _save(m: dict) -> None:
    p = marks_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1), encoding="utf-8")
    tmp.replace(p)


def subjects() -> list[dict]:
    """Round 2: every subject with its preview clips, sorted by letter (letters are random)."""
    rows = []
    for f in sorted((OUT / "pieces").glob("made-part*.json")):
        for s in _load(f, {}).get("subjects", []):
            clips = sorted(c["preview"] for c in s.get("candidates", []) if c.get("preview"))
            clips = [c for c in clips if (OUT / "pieces" / "clips" / c).exists()]
            if clips:
                rows.append({"key": f"{s['niche']}/{s['name']}", "niche": s["niche"], "name": s["name"],
                             "clips": [{"file": c, "letter": c.rsplit("-", 1)[-1][:-4]} for c in clips]})
    return rows


def grids() -> list[dict]:
    """Round 2b: one crop grid per clip. A later part file wins over the whole run: it re-rendered the
    same grid under new letters."""
    rows: dict[str, dict] = {}
    files = [OUT / "crops" / "made.json"] + sorted((OUT / "crops").glob("made-part*.json"))
    for f in files:
        for c in _load(f, {}).get("clips", []):
            if c.get("review") and c.get("key"):
                rows[c["review"]] = {"file": c["review"], "niche": c.get("niche", ""), "name": c.get("name", ""),
                                     "letters": sorted(c["key"])}
    return [r for _, r in sorted(rows.items()) if (OUT / "crops" / "review" / r["file"]).exists()]


def pairs() -> list[dict]:
    """Round 3: the same kind of Short made two ways, under random letters."""
    rows = []
    for f in sorted((OUT / "round3").glob("made-part*.json")):
        for r in _load(f, {}).get("shorts", []):
            files = {}
            for letter, version in r.get("key", {}).items():
                name = (r.get(version) or {}).get("file")
                if name and (OUT / "round3" / "shorts" / name).exists():
                    files[letter] = name
            if files:  # a single one when the other way made nothing: then "good enough?"
                key = f"{r['niche']}-{r['kind']}" + (f"-v{r['remade'] + 1}" if r.get("remade") else "")
                rows.append({"key": key, "niche": r["niche"], "kind": r["kind"],
                             "files": sorted(files.items())})
    return rows


def judge_pairs() -> list[dict]:
    """Footage filter test (meta/collector/judgetest.py): per subject the clip today's check would
    use vs the new judge's, under random letters."""
    rows = _load(OUT / "judgetest" / "made.json", {}).get("pairs", [])
    return [r for r in rows if all((OUT / "pieces" / "clips" / f).exists() for _, f in r.get("files", []))]


@bp.get("/bench/footage/judge/<int:i>")
def judge(i: int):
    pr = judge_pairs()
    if not pr:
        abort(404)
    if i >= len(pr):
        return redirect(url_for("footage_review.index"))
    p = pr[i]
    return render_template("footage_judge.html", p=p, i=i, total=len(pr), mark=_marks().get("judge", {}).get(p["key"], {}))


@bp.post("/bench/footage/judge/<int:i>")
def save_judge(i: int):
    pr = judge_pairs()
    if i >= len(pr):
        abort(404)
    p = pr[i]
    letters = {lab for lab, _ in p["files"]}
    best = request.form.get("best", "")
    if best not in letters and best not in ("same", "bad"):
        abort(400)
    m = _marks()
    m.setdefault("judge", {})[p["key"]] = {
        "best": best, "boring": sorted(set(request.form.getlist("boring")) & letters),
        "wrong": sorted(set(request.form.getlist("wrong")) & letters), "note": request.form.get("note", "")[:500]}
    _save(m)
    return redirect(url_for("footage_review.judge", i=i + 1))


@bp.get("/bench/footage")
def index():
    m = _marks()
    m.setdefault("shorts", {})
    subs, gr, pr, jp = subjects(), grids(), pairs(), judge_pairs()
    return render_template("footage_review.html", subs=subs, grids=gr, pairs=pr, judge_pairs=jp,
                           judge_done=sum(1 for p in jp if p["key"] in m.get("judge", {})),
                           clips_done=sum(1 for s in subs if s["key"] in m["clips"]),
                           crops_done=sum(1 for g in gr if g["file"] in m["crops"]),
                           shorts_done=sum(1 for p in pr if p["key"] in m["shorts"]))


@bp.get("/bench/footage/shorts/<int:i>")
def shorts(i: int):
    pr = pairs()
    if not pr:
        abort(404)
    if i >= len(pr):
        return redirect(url_for("footage_review.index"))
    p = pr[i]
    return render_template("footage_shorts.html", p=p, i=i, total=len(pr), mark=_marks().get("shorts", {}).get(p["key"], {}))


@bp.post("/bench/footage/shorts/<int:i>")
def save_shorts(i: int):
    pr = pairs()
    if i >= len(pr):
        abort(404)
    p = pr[i]
    letters = {lab for lab, _ in p["files"]}
    best = request.form.get("best", "")
    if best not in letters and best not in ("same", "bad"):
        abort(400)
    m = _marks()
    m.setdefault("shorts", {})[p["key"]] = {"best": best, "wrong": sorted(set(request.form.getlist("wrong")) & letters),
                                            "note": request.form.get("note", "")[:500]}
    _save(m)
    return redirect(url_for("footage_review.shorts", i=i + 1))


@bp.get("/bench/footage/clips/<int:i>")
def clips(i: int):
    subs = subjects()
    if not subs:
        abort(404)
    if i >= len(subs):
        return redirect(url_for("footage_review.index"))
    s = subs[i]
    return render_template("footage_clips.html", s=s, i=i, total=len(subs), bad=_marks()["clips"].get(s["key"], []))


@bp.post("/bench/footage/clips/<int:i>")
def save_clips(i: int):
    subs = subjects()
    if i >= len(subs):
        abort(404)
    s = subs[i]
    ok = {c["letter"] for c in s["clips"]}
    bad = sorted(set(request.form.getlist("bad")) & ok)
    m = _marks()
    m["clips"][s["key"]] = bad
    _save(m)
    return redirect(url_for("footage_review.clips", i=i + 1))


@bp.get("/bench/footage/crops/<int:i>")
def crops(i: int):
    gr = grids()
    if not gr:
        abort(404)
    if i >= len(gr):
        return redirect(url_for("footage_review.index"))
    g = gr[i]
    return render_template("footage_crops.html", g=g, i=i, total=len(gr), mark=_marks()["crops"].get(g["file"], {}))


@bp.post("/bench/footage/crops/<int:i>")
def save_crops(i: int):
    gr = grids()
    if i >= len(gr):
        abort(404)
    g = gr[i]
    best = request.form.getlist("best")  # one or more equally good crops, or "same"
    if not best or any(b not in g["letters"] and b != "same" for b in best):
        abort(400)
    m = _marks()
    m["crops"][g["file"]] = {"best": sorted(best), "cut": sorted(set(request.form.getlist("cut")) & set(g["letters"]))}
    _save(m)
    return redirect(url_for("footage_review.crops", i=i + 1))


def _media(folder: Path, name: str):
    if "/" in name or "\\" in name or not name.endswith(".mp4"):
        abort(404)
    f = folder / name
    if not f.exists():
        abort(404)
    return send_file(f, mimetype="video/mp4", conditional=True, max_age=3600)


@bp.get("/bench/footage/clip/<name>")
def clip_file(name: str):
    return _media(OUT / "pieces" / "clips", name)


@bp.get("/bench/footage/short/<name>")
def short_file(name: str):
    return _media(OUT / "round3" / "shorts", name)


@bp.get("/bench/footage/grid/<name>")
def grid_file(name: str):
    return _media(OUT / "crops" / "review", name)
