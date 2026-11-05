"""Job runner on the server: lets Claude start meta jobs without SSH. Locked down on purpose:

- It reads jobs only from `meta/jobs.toml` on `main` of the official repo (its own checkout
  ~/meta-run is reset to origin/main by the cron line before every run).
- It only runs jobs from the fixed ALLOWLIST below: a name maps to one fixed `python -m meta.<x>`
  command; the only thing a job may set is a small count. Never arbitrary shell.
- It runs as the `shorts` user from cron. It never touches ~/shorts (the live program), with one
  exception Roeland asked for (2026-11-04): the fixed "update" job, queued only when he asks for an
  update, resets ~/shorts to origin/main and restarts the dashboard service. Its only sudo is
  `systemctl restart shorts-dashboard`, allowed by /etc/sudoers.d/shorts-update (see
  meta/README.md); without that file the restart is skipped and the log says so.
- Results (log, an index and small preview videos/frames) are pushed to a SEPARATE private repo
  (`Shorts_results`) with a deploy key that only has access to that repo.

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
RESULTS_URL = "git@github.com:roelandsteur1-blip/Shorts_results.git"
PY = HOME / "shorts" / ".venv" / "bin" / "python"

# name -> (module, fixed args, allowed count range or None). {n} is the job's count.
ALLOWLIST = {
    "samples": ("meta.samples", ["--cards", "{n}", "--quizzes", "1", "--rankings", "1"], (1, 3)),
    "textcard": ("meta.textcard", ["--all", "-n", "{n}"], (1, 3)),
    "quiz": ("meta.quiz", ["--all", "-n", "{n}"], (1, 2)),
    "ranking": ("meta.ranking", ["--all", "-n", "{n}"], (1, 2)),
    "scan": ("meta.scan", [], None),
    "probe": ("meta.probe", [], None),  # read-only archive research for the topic-first plan
    "sweep": ("meta.collector.sweep", [], None),  # footage collector research R2+R3, read-only
    "sweepx": ("meta.collector.sweep", ["--extra"], None),  # the keyed archives only (keys saved later)
    "pieces": ("meta.collector.pieces", ["--part", "{n}"], (1, 2)),  # collector research R4-R7, half the subjects each
    "crops": ("meta.collector.crops", ["--part", "{n}"], (1, 3)),  # collector research R6b: crop methods side by side
    "research-tools": ("meta.collector.tools", [], None),  # tools into ~/research-venv (Roeland OK'd 2026-11-04)
    "study": ("meta.study", [], None),  # competitor channel study (meta/study.toml), read-only
    "update": (None, [], None),  # fixed steps in update_live(), not a module
}
SHEET_JOBS = ("probe", "sweep", "sweepx", "study")  # research jobs whose ~/meta-out/<name>/sheets go into the results
JSON_JOBS = ("pieces", "crops", "study")  # research jobs whose ~/meta-out/<name>/*.json go into the results
SYSTEMCTL = "/usr/bin/systemctl"
SERVICE = "shorts-dashboard"
MAX_HOURS = 3
PROGRESS_EVERY = 600  # seconds between progress pushes while a job runs


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
    if module is None:
        return ["update"]
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
    name = job_id.split("-")[-2] if job_id.count("-") >= 2 else ""
    sheets = OUT / name / "sheets"
    if status != "running" and name in SHEET_JOBS and sheets.exists():
        shutil.copytree(sheets, folder / "sheets", dirs_exist_ok=True)
    if name in JSON_JOBS:
        for f in (OUT / name).glob("*.json"):
            shutil.copy(f, folder / f.name)
    jobs = sorted((RESULTS / "jobs").iterdir(), key=lambda p: p.stat().st_mtime)
    for old in jobs[:-5]:
        shutil.rmtree(old / "previews", ignore_errors=True)
    git_results("add", "-A")
    git_results("commit", "-q", "-m", f"{job_id}: {status}", check=False)
    p = git_results("push", "-q", "origin", "HEAD:main", check=False)
    log("results pushed" if p.returncode == 0 else f"push failed: {p.stderr.strip()[-300:]}")


def update_live() -> tuple[str, str]:
    """The "update" job: the live program to origin/main (what the update one-liner does), then
    restart the dashboard. Returns (status, log)."""
    out = []

    def run(cmd: list[str]) -> int:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        out.append(f"$ {' '.join(cmd)}\n{p.stdout}{p.stderr}".rstrip())
        return p.returncode

    live = str(HOME / "shorts")
    before = subprocess.run(["git", "-C", live, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    if run(["git", "-C", live, "fetch", "-q", "origin", "main"]) or run(["git", "-C", live, "reset", "-q", "--hard", "origin/main"]):
        return "failed (git)", "\n".join(out)
    after = subprocess.run(["git", "-C", live, "log", "--oneline", "-1"], capture_output=True, text=True).stdout.strip()
    out.append(f"code: {before} -> {after}")
    if run(["sudo", "-n", SYSTEMCTL, "restart", SERVICE]):
        out.append("restart not allowed yet: run the one-time sudoers command in meta/README.md")
        return "updated, not restarted", "\n".join(out)
    time.sleep(5)
    active = subprocess.run([SYSTEMCTL, "is-active", SERVICE], capture_output=True, text=True).stdout.strip()
    out.append(f"dashboard: {active}")
    return ("done" if active == "active" else f"restarted, dashboard {active}"), "\n".join(out)


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
        if cmd == ["update"]:
            log(f"job {jid}: updating the live program")
            done[jid] = "running"
            STATE.write_text(json.dumps(done, indent=1), encoding="utf-8")
            status, text = update_live()
            done[jid] = status
            STATE.write_text(json.dumps(done, indent=1), encoding="utf-8")
            log(f"job {jid}: {status}")
            publish(jid, status, text, [])
            break
        log(f"job {jid}: running {' '.join(cmd[1:])}")
        done[jid] = "running"
        STATE.write_text(json.dumps(done, indent=1), encoding="utf-8")
        publish(jid, "running", "started", [])
        start = time.time()
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        logfile = OUT / f"job-{jid}.log"
        with open(logfile, "w", encoding="utf-8") as fh:
            p = subprocess.Popen(cmd, cwd=RUN, env=env, stdout=fh, stderr=subprocess.STDOUT)
            last = time.time()
            while p.poll() is None:
                time.sleep(15)
                if time.time() - start > MAX_HOURS * 3600:
                    p.kill()
                    break
                if time.time() - last > PROGRESS_EVERY:  # progress note with the log so far
                    last = time.time()
                    publish(jid, "running", logfile.read_text(encoding="utf-8", errors="replace"), [])
        text = logfile.read_text(encoding="utf-8", errors="replace")
        rc = p.returncode
        status = "timed out" if rc is None or (rc < 0 and time.time() - start > MAX_HOURS * 3600) else (
            "done" if rc == 0 else f"failed ({rc})")
        files = sorted(f for f in OUT.glob("**/*.mp4") if f.stat().st_mtime >= start and ".shot." not in f.name)
        done[jid] = status
        STATE.write_text(json.dumps(done, indent=1), encoding="utf-8")
        log(f"job {jid}: {status}, {len(files)} new videos")
        publish(jid, status, text, files)
        break  # one job per cron run; the next run picks up the next job


if __name__ == "__main__":
    main()
