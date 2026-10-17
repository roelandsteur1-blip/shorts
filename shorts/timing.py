"""How long each step takes: a 'TIMING' log line per step, and a row in the timings table so the
Stats page can show seconds per Short and seconds per minute of footage over time."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)


def record(conn: sqlite3.Connection | None, stage: str, seconds: float, items: int = 1, **info) -> None:
    log.info("TIMING stage=%s seconds=%.1f items=%d%s", stage, seconds, items,
             "".join(f" {k}={v}" for k, v in info.items() if not isinstance(v, (dict, list))))
    if conn is None:
        return
    try:
        conn.execute("INSERT INTO timings (stage, seconds, items, info) VALUES (?,?,?,?)",
                     (stage, round(seconds, 3), items, json.dumps(info, default=str) if info else None))
        conn.commit()
    except sqlite3.Error as e:  # timings must never break the pipeline
        log.debug("could not store timing: %s", e)


@contextmanager
def timed(conn: sqlite3.Connection | None, stage: str, items: int = 1, **info):
    """with timed(conn, "render", short=12): ...  The yielded dict can take extra info."""
    extra: dict = {}
    t0 = time.perf_counter()
    try:
        yield extra
    finally:
        record(conn, stage, time.perf_counter() - t0, items, **info, **extra)


def summary(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    """Per stage over the last `days`: runs, items, total and average seconds per item."""
    rows = conn.execute(
        """SELECT stage, COUNT(*) AS runs, SUM(items) AS items, SUM(seconds) AS seconds
             FROM timings WHERE at >= datetime('now', ?) GROUP BY stage ORDER BY SUM(seconds) DESC""",
        (f"-{days} days",)).fetchall()
    return [{"stage": r["stage"], "runs": r["runs"], "items": r["items"], "seconds": r["seconds"],
             "per_item": r["seconds"] / max(r["items"], 1)} for r in rows]


def claude_usage(conn: sqlite3.Connection, hours: int = 5) -> dict:
    """Claude tokens over the last `hours` (the Pro limit is a rolling 5-hour window)."""
    out = {"calls": 0, "input": 0, "output": 0, "cached": 0}
    for (info,) in conn.execute("""SELECT info FROM timings WHERE stage LIKE 'claude%'
                                     AND at >= datetime('now', ?)""", (f"-{hours} hours",)):
        u = (json.loads(info or "{}").get("usage") or {})
        out["calls"] += 1
        out["input"] += int(u.get("input_tokens") or 0)
        out["output"] += int(u.get("output_tokens") or 0)
        out["cached"] += int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
    return out
