"""Background jobs for the dashboard. Each job is a subprocess `python -m shorts --channel <id> ...`,
so the CLI stays the single source of truth and everything still works without the dashboard.

One heavy job at a time (CPU encoding and the Claude limit are shared); the rest queue.
Light jobs (logging in to YouTube) start right away.

Jobs outlive the dashboard: a restart (server update, crash) leaves them running and the next
dashboard picks them up again (Runner.adopt). On the server this needs KillMode=process in the
systemd unit (deploy/shorts-dashboard.service), else systemd ends them with the dashboard."""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import db, progress
from .config import ROOT, Settings

log = logging.getLogger(__name__)

LIGHT = {"connect", "sources", "stats", "connect-drive", "phone-check"}  # commands that don't need the CPU


def submit(conn: sqlite3.Connection, channel: str | None, args: list[str], label: str | None = None) -> int:
    cur = conn.execute("INSERT INTO jobs (channel, command, label, heavy) VALUES (?,?,?,?)",
                       (channel, json.dumps(args), label or " ".join(args), int(args[0] not in LIGHT)))
    conn.commit()
    return cur.lastrowid


def recover(conn: sqlite3.Connection) -> int:
    """Jobs still marked running whose process is gone: mark them interrupted (or done/failed when
    the process left its exit code). Jobs whose process is still alive are left running: the
    Runner adopts them (a dashboard restart, e.g. a server update, no longer kills a make)."""
    n = 0
    for job in conn.execute("SELECT * FROM jobs WHERE status='running'").fetchall():
        if alive(job):
            continue
        code = exit_code(job)
        status = "interrupted" if code is None else ("done" if code == 0 else "failed")
        conn.execute("UPDATE jobs SET status=?, exit_code=?, finished_at=datetime('now') WHERE id=?",
                     (status, code, job["id"]))
        n += status == "interrupted"
    conn.commit()
    return n


def exit_file(log_path: str | Path) -> Path:
    return Path(str(log_path) + ".exit")


def exit_code(job: sqlite3.Row) -> int | None:
    try:
        return int(exit_file(job["log_path"]).read_text().strip())
    except (TypeError, OSError, ValueError):
        return None


def alive(job: sqlite3.Row) -> bool:
    """Is this job's process (the wrapper shell started by _launch) still running? Checks the
    command line on Linux so a reused pid is not mistaken for the job."""
    pid, log_path = job["pid"], job["log_path"]
    if not pid or not log_path:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:  # no /proc (not Linux): pid check only
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    return str(exit_file(log_path)).encode() in cmdline


class Adopted:
    """A job process started by an earlier dashboard: not our child, so it is followed by pid
    and its exit code is read from the file the wrapper shell writes."""

    def __init__(self, job: sqlite3.Row):
        self.job = job
        self.pid = job["pid"]

    def poll(self) -> int | None:
        if alive(self.job):
            return None
        code = exit_code(self.job)
        return -1 if code is None else code  # gone without an exit code: killed


def tail(path: str | None, lines: int = 40) -> str:
    if not path or not Path(path).exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 64_000))
        text = f.read().decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def follow_up(job: sqlite3.Row) -> tuple[str, list[str]] | None:
    """Button for a job that stopped early: 'Finish' for make (the existing finish command picks up
    where it stopped), 'Run again' for the rest (downloads and scene cutting skip finished work)."""
    if job["status"] not in ("interrupted", "failed", "stopped"):
        return None
    args = json.loads(job["command"])
    if args[0] == "make":
        return "Finish", ["finish"]
    return "Run again", args


def cli_command(settings: Settings, job: sqlite3.Row) -> list[str]:
    cmd = [sys.executable, "-u", "-m", "shorts", "--data-dir", str(settings.data_dir),
           "--config-dir", str(settings.config_dir)]
    if job["channel"]:
        cmd += ["--channel", job["channel"]]
    return cmd + json.loads(job["command"])


class Runner:
    """Starts queued jobs, follows their log for PROGRESS lines and records how they ended.
    `command` builds the argv for a job (tests pass a fake one)."""

    def __init__(self, settings: Settings, command: Callable[[Settings, sqlite3.Row], list[str]] = cli_command,
                 interval: float = 1.0):
        self.settings = settings
        self.command = command
        self.interval = interval
        self.procs: dict[int, tuple[subprocess.Popen | Adopted, int]] = {}  # job id -> (process, log offset)
        self.stopping: set[int] = set()
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.log_dir = settings.data_dir / "logs"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="jobs", daemon=True)
            self._thread.start()

    def adopt(self, conn: sqlite3.Connection) -> int:
        """At start: follow jobs an earlier dashboard started that are still running; the rest
        of the 'running' ones are marked interrupted. Returns how many were adopted."""
        recover(conn)
        with self.lock:
            for job in conn.execute("SELECT * FROM jobs WHERE status='running'").fetchall():
                if job["id"] not in self.procs:
                    self.procs[job["id"]] = (Adopted(job), 0)
            return len(self.procs)

    def _loop(self) -> None:
        conn = db.connect(self.settings.db_path)
        self.adopt(conn)
        # Footage from one-subject sources (NASA) gets its category right away, so the home page
        # counts are right before the first 'make' with this version.
        from .categorize import sort_pending
        try:
            sort_pending(conn, self.settings, fixed_only=True)
        except Exception:  # noqa: BLE001
            log.exception("sorting footage")
        while True:
            try:
                self.tick(conn)
            except Exception:  # noqa: BLE001 - keep the runner alive
                log.exception("job runner")
            time.sleep(self.interval)

    def tick(self, conn: sqlite3.Connection) -> None:
        with self.lock:
            for job_id in list(self.procs):
                self._follow(conn, job_id)
            self._start_next(conn)

    def _start_next(self, conn: sqlite3.Connection) -> None:
        heavy_busy = any(conn.execute("SELECT heavy FROM jobs WHERE id=?", (j,)).fetchone()[0]
                         for j in self.procs)
        for job in conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id").fetchall():
            if job["heavy"] and heavy_busy:
                continue
            self._launch(conn, job)
            heavy_busy |= bool(job["heavy"])

    def _launch(self, conn: sqlite3.Connection, job: sqlite3.Row) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"job-{job['id']:05d}.log"
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        with open(log_path, "wb") as out:
            try:
                # The shell writes the exit code next to the log, so a dashboard started later
                # still learns how the job ended (see Adopted).
                cmd = ["/bin/sh", "-c", '"$@"; c=$?; echo $c > "$0"; exit $c', str(exit_file(log_path)),
                       *self.command(self.settings, job)]
                proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, cwd=str(ROOT), env=env,
                                        start_new_session=True)  # own group: Stop also ends ffmpeg/claude
            except OSError as e:
                out.write(f"could not start: {e}\n".encode())
                conn.execute("""UPDATE jobs SET status='failed', log_path=?, finished_at=datetime('now')
                                 WHERE id=?""", (str(log_path), job["id"]))
                conn.commit()
                return
        conn.execute("""UPDATE jobs SET status='running', pid=?, log_path=?, started_at=datetime('now')
                         WHERE id=?""", (proc.pid, str(log_path), job["id"]))
        conn.commit()
        self.procs[job["id"]] = (proc, 0)

    def _follow(self, conn: sqlite3.Connection, job_id: int) -> None:
        proc, offset = self.procs[job_id]
        code = proc.poll()  # before reading, so the last lines of a finished job are included
        path = conn.execute("SELECT log_path FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        # Only whole lines; a half-written line is read again next tick.
        end = data.rfind(b"\n") + 1 if code is None else len(data)
        last = None
        for line in data[:end].decode("utf-8", errors="replace").splitlines():
            last = progress.parse(line) or last
        if last:
            conn.execute("UPDATE jobs SET progress_done=?, progress_total=?, progress=? WHERE id=?",
                         (*last, job_id))
        self.procs[job_id] = (proc, offset + end)
        if code is not None:
            status = "stopped" if job_id in self.stopping else ("done" if code == 0 else "failed")
            conn.execute("""UPDATE jobs SET status=?, exit_code=?, finished_at=datetime('now')
                             WHERE id=?""", (status, code, job_id))
            del self.procs[job_id]
            self.stopping.discard(job_id)
        conn.commit()

    def stop(self, conn: sqlite3.Connection, job_id: int) -> None:
        with self.lock:
            if job_id in self.procs:
                self.stopping.add(job_id)
                kill_tree(self.procs[job_id][0])
            else:
                conn.execute("""UPDATE jobs SET status='stopped', finished_at=datetime('now')
                                 WHERE id=? AND status='queued'""", (job_id,))
                conn.commit()


def kill_tree(proc: subprocess.Popen | Adopted) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
