"""Structured progress lines for the dashboard's job runner: 'PROGRESS 3/10 render'.

Printed to stdout (the runner reads the job's log file), so they also show in a terminal."""
from __future__ import annotations

import re
import threading

PATTERN = re.compile(r"^PROGRESS (\d+)/(\d+) (.+)$")


_local = threading.local()


def mute() -> None:
    """No progress lines from this thread (background work inside a job, e.g. downloads during
    make, would otherwise make the job's progress bar jump between steps)."""
    _local.muted = True


def report(done: int, total: int, step: str) -> None:
    if getattr(_local, "muted", False):
        return
    print(f"PROGRESS {done}/{total} {step}", flush=True)


def parse(line: str) -> tuple[int, int, str] | None:
    m = PATTERN.match(line.strip())
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else None
