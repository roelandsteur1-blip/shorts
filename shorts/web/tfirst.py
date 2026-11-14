"""The transcript-first dashboard (Roeland, 2026-11-14: the dashboard is now only for the
transcript-first Shorts maker, meta/tclips.py; the classic pages are in archive/classic-dashboard).

Read-only views of what the maker and the server's job runner leave on disk, plus the live database
rows the upload writes (format_id 'tclips'):

- ~/meta-out/tclips/made.json, examples.json, upload-plan.json, sources-<kind>.json, the MP4s
- ~/meta-out/transcripts/<source>.json (cached transcripts)
- ~/meta-out/runner-done.json and ~/meta-run/meta/jobs.toml (the runner's jobs and their state)
- shorts rows with format_id 'tclips', with YouTube's numbers from yt_videos
"""
from __future__ import annotations

import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, current_app, render_template, request, send_file

from .common import conn, settings

bp = Blueprint("tfirst", __name__)

FORMAT_ID = "tclips"
CHANNEL = "historybuffed"
REPO = Path(__file__).resolve().parents[2]
KIND_LABELS = {"newsreel": "Newsreel", "nasa": "NASA", "prelinger": "Prelinger", "feature": "Feature film",
               "classictv": "Classic TV"}
NAME = re.compile(r"[A-Za-z0-9_.-]{1,120}")


# ---------- where things are (overridable for tests through app.config) ----------

def out_dir() -> Path:
    return Path(current_app.config.get("TCLIPS_OUT") or Path.home() / "meta-out" / "tclips")


def meta_out() -> Path:
    return out_dir().parent


def jobs_file() -> Path:
    """The runner's own checkout is reset to main every 5 minutes, so it is newer than ~/shorts."""
    if current_app.config.get("TCLIPS_JOBS"):
        return Path(current_app.config["TCLIPS_JOBS"])
    fresh = Path.home() / "meta-run" / "meta" / "jobs.toml"
    return fresh if fresh.exists() else REPO / "meta" / "jobs.toml"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------- data ----------

def db_rows() -> dict[str, dict]:
    """Uploaded transcript-first Shorts by name (the MP4's stem), with YouTube's numbers."""
    try:
        rows = conn().execute(
            """SELECT s.id, s.channel, s.title, s.status, s.youtube_id, s.publish_at, s.video_path, s.error,
                      y.state AS yt_state, y.views, y.likes, y.comments, y.avg_view_pct, y.engaged_views
                 FROM shorts s LEFT JOIN yt_videos y ON y.youtube_id = s.youtube_id
                WHERE s.format_id = ? ORDER BY s.id""", (FORMAT_ID,)).fetchall()
    except Exception:  # noqa: BLE001 - an empty or older database: show the files anyway
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        name = Path(r["video_path"] or "").stem
        if name and (name not in out or r["status"] in ("scheduled", "uploaded")):
            out[name] = dict(r)
    return out


def video_for(name: str, plan_paths: dict[str, str]) -> Path | None:
    base = out_dir()
    for p in (plan_paths.get(name), base / f"{name}.mp4", base / "examples" / f"{name}.mp4"):
        if p and Path(p).is_file():
            return Path(p)
    return None


def all_shorts() -> list[dict]:
    """Every transcript-first Short the maker wrote down: made.json, examples.json and the upload
    plan, newest file first, one entry per name."""
    base = out_dir()
    plan = read_json(base / "upload-plan.json", [])
    plan_paths = {m.get("short"): m.get("path") for m in plan if m.get("short")}
    rows = db_rows()
    seen: dict[str, dict] = {}
    for group, items in (("upload", plan), ("examples", read_json(base / "examples.json", [])),
                         ("made", read_json(base / "made.json", []))):
        for m in items:
            name = m.get("short")
            if not name or name in seen:
                continue
            row = rows.get(name)
            video = video_for(name, plan_paths)
            seen[name] = {
                "name": name, "group": group, "kind": m.get("kind", ""),
                "kind_label": KIND_LABELS.get(m.get("kind", ""), m.get("kind", "")),
                "title": (row or {}).get("title") or m.get("yt_title") or m.get("hook") or name,
                "hook": m.get("hook") or "", "context": m.get("context") or "", "payoff": m.get("payoff") or "",
                "said": m.get("said") or "", "why": m.get("why") or "",
                "source": m.get("title_source") or m.get("source") or "", "link": m.get("link") or "",
                "year": m.get("year"), "at": m.get("at") or "", "rights": m.get("rights") or "",
                "score": m.get("score"), "secs": next(iter((m.get("lengths") or {}).values()), m.get("secs")),
                "in_plan": name in plan_paths, "row": row, "video": bool(video),
            }
    return list(seen.values())


def state_of(s: dict, now: str) -> str:
    row = s["row"]
    if row and row["youtube_id"]:
        if row.get("yt_state") == "live" or (row["publish_at"] and row["publish_at"] <= now):
            return "live"
        return "scheduled"
    if row and row["status"] == "failed":
        return "failed"
    if s["in_plan"]:
        return "planned"
    return "example" if s["group"] == "examples" else "made"


def runner_jobs(limit: int | None = None) -> list[dict]:
    """jobs.toml (newest last there) with each job's comment and the runner's state, newest first."""
    try:
        text = jobs_file().read_text(encoding="utf-8")
    except OSError:
        return []
    notes: list[str] = []
    jobs: list[dict] = []
    comment: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            comment.append(s.lstrip("#").strip())
        elif s == "[[job]]":
            notes.append(" ".join(comment))
            comment = []
        elif not s:
            continue
        elif not s.startswith(("id", "name", "n ", "n=")):
            comment = []
    try:
        parsed = tomllib.loads(text).get("job", [])
    except ValueError:
        parsed = []
    done = read_json(meta_out() / "runner-done.json", {})
    for i, j in enumerate(parsed):
        jobs.append({"id": j.get("id", ""), "name": j.get("name", ""), "n": j.get("n"),
                     "note": notes[i] if i < len(notes) else "", "state": done.get(j.get("id", ""), "waiting")})
    jobs.reverse()
    return jobs[:limit] if limit else jobs


def transcript_ids() -> set[str]:
    folder = meta_out() / "transcripts"
    return {p.stem for p in folder.glob("*.json")} if folder.is_dir() else set()


def safe_id(source_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", source_id)[:120]


def sources() -> list[dict]:
    """Every long source the maker picked (sources-<kind>.json), plus checked films not picked yet."""
    base = out_dir()
    cached = transcript_ids()
    shorts = all_shorts()
    out, seen = [], set()
    for f in sorted(base.glob("sources-*.json")):
        for s in read_json(f, []):
            sid = s.get("id")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            made = [x for x in shorts if x["source"] in (s.get("title"), sid) or x["link"] == s.get("link")]
            out.append({"id": sid, "kind": s.get("kind", ""), "kind_label": KIND_LABELS.get(s.get("kind", ""), ""),
                        "title": s.get("title") or sid, "year": s.get("year"), "link": s.get("link"),
                        "minutes": round((s.get("length") or 0) / 60), "rights": s.get("rights") or "",
                        "transcript": safe_id(sid) in cached, "shorts": len(made),
                        "up": sum(1 for x in made if x["row"] and x["row"]["youtube_id"])})
    return out


def checked_titles() -> tuple[list[dict], list[str]]:
    path = settings().config_dir / "clip-titles.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    return data.get("title", []), data.get("deny", {}).get("words", [])


# ---------- pages ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@bp.get("/")
def home():
    now = now_iso()
    shorts = all_shorts()
    for s in shorts:
        s["state"] = state_of(s, now)
    count = {k: sum(1 for s in shorts if s["state"] == k) for k in ("live", "scheduled", "planned", "made", "example", "failed")}
    upcoming = sorted((s for s in shorts if s["state"] == "scheduled"), key=lambda s: s["row"]["publish_at"] or "")
    live = sorted((s for s in shorts if s["state"] == "live"), key=lambda s: s["row"]["publish_at"] or "", reverse=True)
    views = sum((s["row"] or {}).get("views") or 0 for s in live)
    srcs = sources()
    jobs = runner_jobs()
    running = [j for j in jobs if j["state"] == "running"]
    waiting = [j for j in jobs if j["state"] == "waiting"]
    return render_template("t_home.html", count=count, upcoming=upcoming[:7], live=live[:10], views=views,
                           n_sources=len(srcs), n_transcripts=sum(s["transcript"] for s in srcs),
                           running=running, waiting=waiting, recent=jobs[:8], total=len(shorts), channel=CHANNEL)


STATES = {"all": "All", "live": "Live", "scheduled": "Scheduled", "planned": "In the upload plan",
          "made": "Made, not used", "example": "Examples", "failed": "Failed"}


@bp.get("/shorts")
def shorts_page():
    now = now_iso()
    shorts = all_shorts()
    for s in shorts:
        s["state"] = state_of(s, now)
    pick = request.args.get("state", "all")
    pick = pick if pick in STATES else "all"
    kind = request.args.get("kind", "")
    counts = {k: (len(shorts) if k == "all" else sum(1 for s in shorts if s["state"] == k)) for k in STATES}
    shown = [s for s in shorts if (pick == "all" or s["state"] == pick) and (not kind or s["kind"] == kind)]
    shown.sort(key=lambda s: ((s["row"] or {}).get("publish_at") or "9", s["name"]))
    kinds = sorted({s["kind"] for s in shorts if s["kind"]})
    return render_template("t_shorts.html", shorts=shown, states=STATES, counts=counts, pick=pick, kind=kind,
                           kinds=kinds, labels=KIND_LABELS)


@bp.get("/shorts/<name>")
def short_page(name: str):
    now = now_iso()
    for s in all_shorts():
        if s["name"] == name:
            s["state"] = state_of(s, now)
            return render_template("t_short.html", s=s)
    abort(404)


@bp.get("/video/<name>.mp4")
def video(name: str):
    if not NAME.fullmatch(name):
        abort(404)
    plan = read_json(out_dir() / "upload-plan.json", [])
    path = video_for(name, {m.get("short"): m.get("path") for m in plan if m.get("short")})
    if not path:
        abort(404)
    return send_file(path, mimetype="video/mp4", conditional=True, max_age=3600)


@bp.get("/schedule")
def schedule():
    now = now_iso()
    rows = sorted(db_rows().values(), key=lambda r: r["publish_at"] or "9")
    for r in rows:
        r["live"] = bool(r["youtube_id"] and r["publish_at"] and r["publish_at"] <= now)
        r["name"] = Path(r["video_path"] or "").stem
    return render_template("t_schedule.html", rows=rows, channel=CHANNEL)


@bp.get("/sources")
def sources_page():
    titles, deny = checked_titles()
    return render_template("t_sources.html", sources=sources(), titles=titles, deny=deny, labels=KIND_LABELS)


@bp.get("/jobs")
def jobs_page():
    return render_template("t_jobs.html", jobs=runner_jobs(60))
