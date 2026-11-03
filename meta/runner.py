"""Job runner on the server: lets Claude start meta jobs without SSH. Locked down on purpose:

- It reads jobs only from `meta/jobs.toml` on `main` of the official repo (its own checkout
  ~/meta-run is reset to origin/main by the cron line before every run).
- It only runs jobs from the fixed ALLOWLIST below: a name maps to one fixed `python -m meta.<x>`
  command; the only thing a job may set is a small count. Never arbitrary shell.
- It runs as the `shorts` user from cron, without sudo. It never touches ~/shorts (the live
  program): no git reset there, no restarts, no writes to its database.
- Results (log, an index and small preview videos/frames) are pushed to a SEPARATE private repo
  (`shorts-results`) with a deploy key that only has access to that repo.

Cron (every 5 minutes, installed by meta/setup-runner.sh):
    cd ~/meta-run && git fetch -q origin main && git reset -q --hard origin/main &&
    META_CONFIG_DIR=~/shorts/config ~/shorts/.venv/bin/python -m meta.runner
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
RUN = HOME / "meta-run"                 # this checkout (main), used only for meta
OUT = HOME / "meta-out"
STATE = OUT / "runner-done.json"
RESULTS = HOME / "meta-results"         # local clone of the private results repo
KEY = HOME / ".ssh" / "meta_results_deploy"
RESULTS_URL = "git@github.com:roelandsteur1-blip/shorts-results.git"
PY = HOME / "shorts" / ".venv" / "bin" / "python"

# name -> (module, fixed args, allowed count range or None). {n} is the job's count.
ALLOWLIST = {
    "samples": ("meta.samples", ["--cards", "{n}", "--quizzes", "1", "--rankings", "1"], (1, 3)),
    "textcard": ("meta.textcard", ["--all", "-n", "{n}"], (1, 3)),
    "quiz": ("meta.quiz", ["--all", "-n", "{n}"], (1, 2)),
    "ranking": ("meta.ranking", ["--all", "-n", "{n}"], (1, 2)),
    "scan": ("meta.scan", [], None),
}
MAX_HOURS = 3


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_jobs() -> list[dict]:
    path = RUN / "meta" / "jobs.toml"
    if not path.exists():
        return []
    return tomllib.loads(path.read_text(encoding="utf-8")).get("job", [])


def command(job: dict) -> list[str] | None:
    """The fixed command for an allowlisted job, or None when the job isn't allowed."""
    name = job.get("name")
    if name not in ALLOWLIST or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", str(job.get("id", ""))):
        return None
    module, args, limits = ALLOWLIST[name]
    n = job.get("n", 1)
    if limits:
        if not isinstance(n, int) or not limits[0] <= n <= limits[1]:
            return None
    return [str(PY), "-m", module] + [a.replace("{n}", str(n)) for a in args]


def git_results(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_SSH_COMMAND=f"ssh -i {KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new")
    return subprocess.run(["git", "-C", str(RESULTS), *args], env=env, capture_output=True, text=True,
                          check=check)


def previews(files: list[Path], dest: Path) -> None:
    """Small 540x960 copies of new videos plus one sheet of frames, so results stay a few MB."""
    dest.mkdir(parents=True, exist_ok=True)
    frames = []
    for f in files:
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(f), "-vf",
                            "scale=540:960", "-c:v", "libx264", "-crf", "32", "-preset", "veryfast",
                            "-c:a", "aac", "-b:a", "64k", str(dest / f.name)], check=True, timeout=300)
            jpg = dest / (f.stem + ".jpg")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "4", "-i", str(f),
                            "-frames:v", "1", "-vf", "scale=360:640", str(jpg)], check=True, timeout=120)
            frames.append(jpg)
        except (subprocess.SubprocessError, OSError) as e:
            log(f"preview of {f.name} failed: {e}")


def publish(job_id: str, status: str, log_text: str, files: list[Path]) -> None:
    """Push this job's results to the private results repo (one folder per job; old previews
    beyond the last 5 jobs are dropped to keep the repo small)."""
    if not KEY.exists():
        log("no results deploy key yet: results stay in ~/meta-out")
        return
    if not (RESULTS / ".git").exists():
        RESULTS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(RESULTS)], check=True)
        git_results("remote", "add", "origin", RESULTS_URL)
        git_results("config", "user.name", "shorts server")
        git_results("config", "user.email", "server@localhost")
    git_results("fetch", "-q", "origin", "main", check=False)
    if git_results("rev-parse", "--verify", "origin/main", check=False).returncode == 0:
        git_results("reset", "-q", "--hard", "origin/main")
    folder = RESULTS / "jobs" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "log.txt").write_text(log_text[-200_000:], encoding="utf-8")
    (folder / "status.json").write_text(json.dumps({
        "id": job_id, "status": status, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": [str(f) for f in files]}, indent=1), encoding="utf-8")
    for idx in OUT.glob("**/made.json"):
        shutil.copy(idx, folder / f"made-{idx.parent.name}.json")
    if files:
        previews(files, folder / "previews")
    jobs = sorted((RESULTS / "jobs").iterdir(), key=lambda p: p.stat().st_mtime)
    for old in jobs[:-5]:
        shutil.rmtree(old / "previews", ignore_errors=True)
    git_results("add", "-A")
    git_results("commit", "-q", "-m", f"{job_id}: {status}", check=False)
    p = git_results("push", "-q", "origin", "HEAD:main", check=False)
    log("results pushed" if p.returncode == 0 else f"push failed: {p.stderr.strip()[-300:]}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    done = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    for job in load_jobs():
        jid = str(job.get("id"))
        if jid in done:
            continue
        cmd = command(job)
        if not cmd:
            done[jid] = "refused"
            STATE.write_text(json.dumps(done, indent=1), encoding="utf-8")
            log(f"job {jid}: refused (not on the allowlist or bad count)")
            publish(jid, "refused", "not on the allowlist or bad count", [])
            continue
        log(f"job {jid}: running {' '.join(cmd[1:])}")
        done[jid] = "running"
        STATE.write_text(json.dumps(done, indent=1), encoding="utf-8")
        publish(jid, "running", "started", [])
        start = time.time()
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            p = subprocess.run(cmd, cwd=RUN, env=env, capture_output=True, text=True, errors="replace",
                               timeout=MAX_HOURS * 3600)
            status, text = ("done" if p.returncode == 0 else f"failed ({p.returncode})"), p.stdout + p.stderr
        except subprocess.TimeoutExpired as e:
            status, text = "timed out", str(e.stdout or "") + str(e.stderr or "")
        files = sorted(f for f in OUT.glob("**/*.mp4") if f.stat().st_mtime >= start)
        done[jid] = status
        STATE.write_text(json.dumps(done, indent=1), encoding="utf-8")
        log(f"job {jid}: {status}, {len(files)} new videos")
        publish(jid, status, text, files)
        break  # one job per cron run; the next run picks up the next job


if __name__ == "__main__":
    main()
