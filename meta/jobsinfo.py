"""Read-only look at the dashboard's recent jobs and the service's restarts, to see why a job ended.

    python -m meta.jobsinfo

Prints the last 30 jobs (status, times, progress, exit code), the last log lines of each job that
did not finish cleanly, and the dashboard service's start/stop lines from the journal (when this
user may read it) plus memory-killer lines. Never changes anything.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from . import config_dir


def run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"({e})"


def main() -> None:
    from shorts.config import load_settings
    settings = load_settings(config_dir())
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 30").fetchall()
    print("== last 30 jobs (times UTC)")
    for r in rows:
        args = " ".join(json.loads(r["command"]))
        print(f"#{r['id']} {r['channel'] or '-'} [{args}] {r['status']} exit={r['exit_code']} "
              f"created={r['created_at']} started={r['started_at']} finished={r['finished_at']} "
              f"progress={r['progress_done']}/{r['progress_total']} {r['progress'] or ''}")
    for r in rows[:15]:
        if r["status"] in ("interrupted", "failed") and r["log_path"] and Path(r["log_path"]).exists():
            lines = Path(r["log_path"]).read_text(encoding="utf-8", errors="replace").splitlines()
            print(f"\n== log tail of #{r['id']} ({r['status']})")
            print("\n".join(lines[-25:]))
    print("\n== service")
    print(run(["systemctl", "show", "shorts-dashboard", "-p", "ActiveEnterTimestamp,NRestarts,ExecMainStartTimestamp,KillMode"]))
    print("\n== journal: service starts/stops (last 2 days)")
    print(run(["journalctl", "-u", "shorts-dashboard", "--since", "-2d", "--no-pager", "-o", "short-iso",
               "-g", "Start|Stop|Kill|kill|Main process|Failed|memory|OOM|left-over|Consumed"])[-8000:])
    print("\n== journal: memory killer (last 2 days)")
    print(run(["journalctl", "-k", "--since", "-2d", "--no-pager", "-o", "short-iso", "-g", "oom|Out of memory|Killed process"])[-4000:])
    print("\n== uptime / memory")
    print(run(["uptime"]))
    print(run(["free", "-m"]))


if __name__ == "__main__":
    main()
