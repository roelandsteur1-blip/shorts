"""Structured progress lines for the dashboard's job runner: 'PROGRESS 3/10 render'.

Printed to stdout (the runner reads the job's log file), so they also show in a terminal."""
from __future__ import annotations

import re

PATTERN = re.compile(r"^PROGRESS (\d+)/(\d+) (.+)$")


def report(done: int, total: int, step: str) -> None:
    print(f"PROGRESS {done}/{total} {step}", flush=True)


def parse(line: str) -> tuple[int, int, str] | None:
    m = PATTERN.match(line.strip())
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else None
