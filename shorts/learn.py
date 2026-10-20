"""How each Short did against its own channel's usual, for the parts of the pipeline that learn
from results (music picks, formats, length, footage).

A Short's score is log2 of its engaged views over the median of its channel's Shorts at the same
age (3 days, else 1 day): +1 = twice the usual, -1 = half. Channels are compared with themselves,
so a big channel doesn't make its formats or tracks look good. Group scores are shrunk toward 0
while few Shorts have numbers, so one lucky Short isn't a pattern."""
from __future__ import annotations

import sqlite3
from typing import Callable

import numpy as np

MIN_CHANNEL = 3   # Shorts with numbers a channel needs before its usual means anything
PRIOR = 2         # pretend every group also has this many average Shorts
LIMIT = 2.0       # log2 cap either way (4x better or worse)


def short_scores(conn: sqlite3.Connection, channel: str | None = None) -> list[dict]:
    """Every Short with numbers, once (at the longest age it has): its shorts row + 'score'."""
    rows = conn.execute(
        f"""SELECT s.*, a.days AS age_days, a.engaged_views AS age_engaged FROM stats_age a
              JOIN shorts s ON s.id = a.short_id
             WHERE a.engaged_views IS NOT NULL AND NOT s.stats_hidden AND a.days IN (1, 3)
                   {"AND s.channel = ?" if channel else ""}
             ORDER BY a.days DESC""", (channel,) if channel else ()).fetchall()
    usual: dict[tuple[str, int], list[int]] = {}
    for r in rows:
        usual.setdefault((r["channel"], r["age_days"]), []).append(r["age_engaged"])
    out, done = [], set()
    for r in rows:  # 3 days first
        vals = usual[(r["channel"], r["age_days"])]
        if r["id"] in done or len(vals) < MIN_CHANNEL:
            continue
        done.add(r["id"])
        ratio = (r["age_engaged"] + 1) / (float(np.median(vals)) + 1)
        out.append(dict(r) | {"score": float(np.clip(np.log2(ratio), -LIMIT, LIMIT))})
    return out


def group_scores(shorts: list[dict], key: Callable[[dict], object]) -> dict:
    """{group: {'score', 'shorts'}}; key(short) -> group (None = leave the Short out)."""
    groups: dict = {}
    for s in shorts:
        k = key(s)
        if k is not None:
            groups.setdefault(k, []).append(s["score"])
    return {k: {"score": sum(v) / (len(v) + PRIOR), "shorts": len(v)} for k, v in groups.items()}


def percent(score: float) -> str:
    """+1.0 -> '+100%'."""
    return f"{(2 ** score - 1) * 100:+.0f}%"


# ---------- retention: where people stop watching ----------

MIN_CURVES = 6        # Shorts with a retention curve before the prompt hears about it
SHOWN = 3


def _watch_at(curve: list, x: float) -> float:
    xs = [p[0] for p in curve]
    return float(np.interp(x, xs, [p[1] for p in curve]))


def block_retention(conn: sqlite3.Connection, channel: str | None = None) -> list[dict]:
    """Per block of every Short with a retention curve: share still watching at its start and end,
    and 'drop', points lost per second relative to its own Short's average (positive = it lost
    people faster than the rest of that Short)."""
    rows = conn.execute(
        f"""SELECT r.short_id, r.curve, s.hook, s.channel FROM retention r JOIN shorts s ON s.id = r.short_id
             WHERE NOT s.stats_hidden {"AND s.channel = ?" if channel else ""}""",
        (channel,) if channel else ()).fetchall()
    out = []
    import json
    for r in rows:
        curve = json.loads(r["curve"])
        blocks = conn.execute(
            """SELECT b.idx, b.text, b.seconds, b.scene_id, a.source,
                      (SELECT category FROM scene_categories c WHERE c.scene_id = b.scene_id AND c.confidence > 0
                        ORDER BY c.confidence DESC LIMIT 1) AS category
                 FROM blocks b LEFT JOIN scenes sc ON sc.id = b.scene_id LEFT JOIN assets a ON a.id = sc.asset_id
                WHERE b.short_id = ? ORDER BY b.idx""", (r["short_id"],)).fetchall()
        total = sum(b["seconds"] or 0 for b in blocks)
        if not curve or total <= 0:
            continue
        t, mine = 0.0, []
        for b in blocks:
            a, z = _watch_at(curve, t / total), _watch_at(curve, (t + b["seconds"]) / total)
            mine.append({"short_id": r["short_id"], "hook": r["hook"], "idx": b["idx"], "text": b["text"],
                         "source": b["source"], "category": b["category"], "start": a, "end": z,
                         "per_s": (a - z) / max(b["seconds"], 0.1)})
            t += b["seconds"]
        avg = float(np.mean([m["per_s"] for m in mine]))
        for m in mine:
            m["drop"] = m["per_s"] - avg
        out += mine
    return out


def retention_note(conn: sqlite3.Connection, channel: str) -> str:
    rows = block_retention(conn, channel)
    shorts = {r["short_id"] for r in rows}
    if len(shorts) < MIN_CURVES:
        return ""
    after_hook = [r["end"] for r in rows if r["idx"] == 0]
    later = [r for r in rows if r["idx"] > 0]
    worst = sorted(later, key=lambda r: -r["drop"])[:SHOWN]
    best = sorted(later, key=lambda r: r["drop"])[:SHOWN]
    hooks = sorted((r for r in rows if r["idx"] == 0), key=lambda r: -r["end"])
    n = min(SHOWN, len(hooks) // 2)
    hook = lambda r: f'- "{r["text"]}" ({r["end"] * 100:.0f}% still watching after it)'  # noqa: E731
    line = lambda r: f'- "{r["text"]}" (block {r["idx"] + 1} of the Short with hook "{r["hook"]}")'  # noqa: E731
    return (f"Where viewers stop watching on this channel ({len(shorts)} Shorts): after the hook a median "
            f"{np.median(after_hook) * 100:.0f}% are still there. Hooks that kept the most viewers:\n"
            + "\n".join(map(hook, hooks[:n])) + "\nHooks that lost the most:\n" + "\n".join(map(hook, hooks[-n:]))
            + "\nBlocks that lost viewers fastest:\n"
            + "\n".join(map(line, worst)) + "\nBlocks that held them best:\n" + "\n".join(map(line, best))
            + "\nWrite blocks like the ones that held.\n\n")


def footage_scores(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """{('source'|'category', name): {'score', 'shorts'}}: how blocks with footage from each archive
    and of each category held viewers (log2-ish: positive = lost fewer people than the rest of their
    Short). Used as a small nudge when footage is picked."""
    rows = block_retention(conn)
    out = {}
    for kind in ("source", "category"):
        groups: dict[str, list[float]] = {}
        for r in rows:
            if r["idx"] > 0 and r[kind]:
                # points per second -> a score around -1..1 (5 points a second faster is a lot)
                groups.setdefault(r[kind], []).append(float(np.clip(-r["drop"] / 0.05, -1, 1)))
        for k, v in groups.items():
            out[(kind, k)] = {"score": sum(v) / (len(v) + PRIOR), "shorts": len(v)}
    return out


# ---------- length and titles ----------

LENGTHS = [(0, 30, "under 30 s"), (30, 45, "30-45 s"), (45, 1e9, "over 45 s")]
MIN_GROUP = 3


def _length(s: dict) -> str | None:
    d = s.get("duration")
    return next((name for lo, hi, name in LENGTHS if d and lo <= d < hi), None)


TITLE_TRAITS = {
    "a question": lambda t: t.strip().endswith("?"),
    "a number": lambda t: any(c.isdigit() for c in t),
    '"you" or "your"': lambda t: any(w in ("you", "your") for w in t.lower().replace("'", " ").split()),
}


def patterns_note(conn: sqlite3.Connection, channel: str) -> str:
    """What the numbers say about length and title style on this channel, once each side of a
    comparison has MIN_GROUP Shorts. Empty until then."""
    shorts = short_scores(conn, channel)
    lines = []
    lens = {k: v for k, v in group_scores(shorts, _length).items() if v["shorts"] >= MIN_GROUP}
    if len(lens) >= 2:
        best = max(lens, key=lambda k: lens[k]["score"])
        each = ", ".join(f"{k}: {percent(v['score'])}" for k, v in lens.items())
        lines.append(f"- Length: Shorts of {best} do best here ({each}); aim for that length when the story allows.")
    for trait, test in TITLE_TRAITS.items():
        g = group_scores(shorts, lambda s: bool(test(s.get("title") or "")))
        if all(g.get(x, {}).get("shorts", 0) >= MIN_GROUP for x in (True, False)):
            diff = g[True]["score"] - g[False]["score"]
            if abs(diff) >= 0.2:
                lines.append(f"- Titles with {trait} do {percent(diff)} vs titles without "
                             f"({g[True]['shorts']} vs {g[False]['shorts']} Shorts).")
    if not lines:
        return ""
    return ("Patterns in this channel's results (engaged views against its usual); lean toward what works, "
            "but keep variety:\n" + "\n".join(lines) + "\n\n")
