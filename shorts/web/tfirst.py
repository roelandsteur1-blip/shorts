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
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, request, send_file

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
    chan = [base / "channels" / name.split("-", 1)[0] / f"{name}.mp4"] if "-" in name else []
    for p in (plan_paths.get(name), base / f"{name}.mp4", base / "examples" / f"{name}.mp4", *chan):
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
    chan_rows = [m for f in sorted((base / "channels").glob("*/made.json")) for m in read_json(f, [])]
    for group, items in (("upload", plan), ("channel", chan_rows), ("examples", read_json(base / "examples.json", [])),
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
                "channel": m.get("channel") or (CHANNEL if name in plan_paths or row else ""),
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


# ---------- channels and the Make button (Roeland 2026-11-14) ----------

JOB_NAME = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z0-9_-]{1,40}")
MAX_N = 10


def tchannels() -> list[dict]:
    """config/tchannels.toml (the maker reads the same file)."""
    try:
        data = tomllib.loads((settings().config_dir / "tchannels.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in data.get("channel", []) if re.fullmatch(r"[a-z0-9_-]{1,40}", str(c.get("id", "")))]


def jobs_dir(base: Path | None = None) -> Path:
    return (base or out_dir()) / "dashjobs"


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a finished child we started is a zombie until reaped
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def job_state(meta: dict, folder: Path) -> str:
    exit_file = folder / f"{meta['id']}.exit"
    if exit_file.exists():
        code = exit_file.read_text().strip()
        return "done" if code == "0" else f"failed ({code})"
    if meta.get("pid") is None:
        return "queued"
    return "running" if pid_alive(meta["pid"]) else "stopped"


def dash_jobs(base: Path | None = None) -> list[dict]:
    """Make jobs started from the dashboard, newest first, with their state and last log lines."""
    folder = jobs_dir(base)
    out = []
    for f in sorted(folder.glob("*.json"), reverse=True):
        meta = read_json(f, None)
        if not meta or not JOB_NAME.fullmatch(meta.get("id", "")):
            continue
        meta["state"] = job_state(meta, folder)
        try:
            lines = (folder / f"{meta['id']}.log").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        meta["tail"] = "\n".join(lines[-12:])
        if meta.get("kind") == "connect":
            meta["full"] = "\n".join(lines[-60:])
        meta["ready"] = sum("[channel] READY" in ln for ln in lines)
        out.append(meta)
    out.sort(key=lambda j: j.get("seq") or 0, reverse=True)
    return out


def queue_job(base: Path, channel: str, n: int, **extra) -> dict:
    folder = jobs_dir(base)
    folder.mkdir(parents=True, exist_ok=True)
    jid = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{channel}"
    meta = {"id": jid, "channel": channel, "n": n, "pid": None, "queued_at": now_iso(), "seq": time.time_ns(), **extra}
    (folder / f"{jid}.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def command_for(meta: dict) -> list[str]:
    if meta.get("kind") == "connect":
        return [sys.executable, "-m", "shorts", "--channel", meta["channel"], "connect",
                *(["--fresh"] if meta.get("fresh") else [])]
    return [sys.executable, "-m", "meta.tclips", "channel", "--channel", meta["channel"], "-n", str(meta["n"])]


def launch(base: Path, meta: dict) -> dict:
    """Run a job in its own session (a dashboard restart doesn't stop it), at low priority; its exit
    code lands in <id>.exit and its output in <id>.log."""
    folder = jobs_dir(base)
    log_f = open(folder / f"{meta['id']}.log", "ab")
    exit_f = folder / f"{meta['id']}.exit"
    script = 'nice -n 10 "$@"; echo $? > "$EXIT_FILE"'
    env = dict(os.environ, EXIT_FILE=str(exit_f), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(["sh", "-c", script, "sh", *command_for(meta)], cwd=str(REPO), stdout=log_f,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    log_f.close()
    clean = {k: meta.get(k) for k in ("id", "channel", "n", "queued_at", "seq", "kind", "fresh")}
    clean.update(pid=proc.pid, started_at=now_iso())
    (folder / f"{meta['id']}.json").write_text(json.dumps(clean), encoding="utf-8")
    return clean


def start_next(base: Path) -> dict | None:
    """Start the oldest queued make when no make is running: one make at a time. Connect jobs don't
    queue (they only wait for a login) and don't count."""
    jobs = [j for j in dash_jobs(base) if j.get("kind") != "connect"]
    if any(j["state"] == "running" for j in jobs):
        return None
    queued = [j for j in jobs if j["state"] == "queued"]
    return launch(base, queued[-1]) if queued else None


def stop_pid(pid: int | None) -> None:
    import signal
    if pid and pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass


def start_queue_thread(app) -> None:
    """Every 20 s: start the next queued make when the last one has ended."""
    def loop():
        while True:
            try:
                with app.app_context():
                    start_next(out_dir())
            except Exception:  # noqa: BLE001 - keep the loop alive
                pass
            time.sleep(20)
    threading.Thread(target=loop, name="tclips-queue", daemon=True).start()


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


SORTS = {"channel": "Channel", "newest": "Newest", "publish": "Publish time"}


def channel_names() -> dict[str, str]:
    names = {c["id"]: c.get("name") or c["id"] for c in tchannels()}
    names.setdefault(CHANNEL, CHANNEL)
    return names


@bp.get("/shorts")
def shorts_page():
    now = now_iso()
    shorts = all_shorts()
    for s in shorts:
        s["state"] = state_of(s, now)
    pick = request.args.get("state", "all")
    pick = pick if pick in STATES else "all"
    kind = request.args.get("kind", "")
    chan = request.args.get("channel", "")
    sort = request.args.get("sort", "channel")
    sort = sort if sort in SORTS else "channel"
    names = channel_names()
    counts = {k: (len(shorts) if k == "all" else sum(1 for s in shorts if s["state"] == k)) for k in STATES}
    shown = [s for s in shorts if (pick == "all" or s["state"] == pick) and (not kind or s["kind"] == kind)
             and (not chan or s["channel"] == chan)]
    order = list(names)
    pub = lambda s: (s["row"] or {}).get("publish_at") or "9"  # noqa: E731
    if sort == "publish":
        shown.sort(key=lambda s: (pub(s), s["name"]))
    else:
        shown.sort(key=lambda s: s["name"], reverse=True)  # names carry the make time: newest first
        if sort == "channel":
            shown.sort(key=lambda s: order.index(s["channel"]) if s["channel"] in order else len(order))
    groups: list[tuple[str, list[dict]]] = []
    for s in shown:
        label = names.get(s["channel"], "Tests and examples") if sort == "channel" else ""
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(s)
    per_chan = {c: sum(1 for s in shorts if s["channel"] == c) for c in names}
    kinds = sorted({s["kind"] for s in shorts if s["kind"]})
    return render_template("t_shorts.html", groups=groups, total=len(shown), states=STATES, counts=counts, pick=pick,
                           kind=kind, kinds=kinds, labels=KIND_LABELS, chan=chan, sort=sort, sorts=SORTS,
                           chans=[(c, names[c], n) for c, n in per_chan.items() if n])


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


@bp.get("/channels")
def channels_page():
    chans = tchannels()
    shorts = all_shorts()
    now = now_iso()
    jobs = dash_jobs()
    for c in chans:
        mine = [x for x in shorts if x["channel"] == c["id"]]
        for x in mine:
            x["state"] = state_of(x, now)
        c["shorts"] = sorted(mine, key=lambda x: x["name"], reverse=True)
        c["jobs"] = [j for j in jobs if j["channel"] == c["id"] and j.get("kind") != "connect"][:3]
        c["busy"] = any(j["state"] in ("running", "queued") for j in c["jobs"])
        c["yt"] = youtube_state(c["id"], jobs)
    running = next((j for j in jobs if j["state"] == "running" and j.get("kind") != "connect"), None)
    return render_template("t_channels.html", chans=chans, running=running, jobs=jobs[:10], max_n=MAX_N,
                           labels=KIND_LABELS, msg=request.args.get("msg"))


@bp.post("/channels/<cid>/make")
def channel_make(cid: str):
    if not any(c["id"] == cid for c in tchannels()):
        abort(404)
    try:
        n = max(1, min(int(request.form.get("n", 1)), MAX_N))
    except ValueError:
        n = 1
    base = out_dir()
    queue_job(base, cid, n)
    started = start_next(base)
    msg = (f"Started: {n} Short{'s' if n > 1 else ''} for {cid}." if started and started["channel"] == cid
           else f"Queued: {n} Short{'s' if n > 1 else ''} for {cid}; starts when the running make is done.")
    return redirect(f"/channels?msg={msg}#{cid}")


# ---------- Connect YouTube, the same way the old dashboard did (Roeland 2026-11-14) ----------
# Each channel gets a channel file config/channels/<id>.toml (video_type "transcript", made here on
# the first Connect). Connect runs `python -m shorts --channel <id> connect`: Google's login link
# appears in the log on the Channels page; with `ssh -L 8765:localhost:8765 <server>` running on
# the laptop, opening it there logs in. Then "Is this the right channel?" -> Yes saves its id, and
# uploads refuse any other channel (shorts/upload.py check_channel).

def channel_file(cid: str) -> Path:
    return settings().config_dir / "channels" / f"{cid}.toml"


def ensure_channel_file(c: dict) -> None:
    path = channel_file(c["id"])
    if path.exists():
        return
    from .. import tomlw
    from ..channels import HEADER
    path.parent.mkdir(parents=True, exist_ok=True)
    tomlw.write(path, {"id": c["id"], "name": c.get("name") or c["id"], "language": "en",
                       "niche": c.get("pitch") or c.get("name") or c["id"], "video_type": "transcript",
                       "search_terms": [], "formats": [], "style": {}, "uploads_per_day": 1,
                       "publish_times_utc": ["16:00"]}, HEADER)


def youtube_state(cid: str, jobs: list[dict]) -> dict:
    from ..upload import found_path, token_path
    st = settings()
    data = {}
    if channel_file(cid).exists():
        try:
            data = tomllib.loads(channel_file(cid).read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    found = read_json(found_path(st, cid), None)
    job = next((j for j in jobs if j["channel"] == cid and j.get("kind") == "connect"), None)
    link = None
    if job and job["state"] == "running":
        m = re.search(r"https://accounts\.google\.com/\S+", job.get("full") or "")
        link = m.group(0) if m else None
    return {"confirmed": data.get("youtube_channel_id"), "found": found, "logged_in": token_path(st, cid).exists(),
            "job": job, "link": link}


@bp.post("/channels/<cid>/connect")
def connect_youtube(cid: str):
    c = next((c for c in tchannels() if c["id"] == cid), None)
    if not c:
        abort(404)
    ensure_channel_file(c)
    base = out_dir()
    # A login still waiting for Google (any channel) holds the server's login port 8765: stop it first.
    for j in dash_jobs(base):
        if j.get("kind") == "connect" and j["state"] == "running":
            stop_pid(j["pid"])
    launch(base, queue_job(base, cid, 0, kind="connect", fresh=bool(request.form.get("fresh"))))
    return redirect(f"/channels?msg=Connect started for {c.get('name', cid)}: open the login link below.#{cid}")


@bp.post("/channels/<cid>/confirm")
def confirm_youtube(cid: str):
    from .. import tomlw
    from ..channels import HEADER
    from ..upload import found_path
    if not any(c["id"] == cid for c in tchannels()) or not channel_file(cid).exists():
        abort(404)
    found = read_json(found_path(settings(), cid), None)
    if not found:
        abort(400)
    data = tomllib.loads(channel_file(cid).read_text(encoding="utf-8"))
    data["youtube_channel_id"] = found["id"]
    tomlw.write(channel_file(cid), data, HEADER)
    return redirect(f"/channels?msg=Connected to {found.get('title')}.#{cid}")


@bp.post("/channels/jobs/<jid>/stop")
def stop_job(jid: str):
    if not JOB_NAME.fullmatch(jid):
        abort(404)
    folder = jobs_dir()
    meta = read_json(folder / f"{jid}.json", None)
    if not meta:
        abort(404)
    if meta.get("pid") is None:
        (folder / f"{jid}.json").unlink(missing_ok=True)  # still queued: just drop it
    else:
        stop_pid(meta["pid"])
    return redirect("/channels?msg=Stopped.")


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
