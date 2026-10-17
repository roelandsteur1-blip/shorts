"""Second, separate Claude call that checks every claim against its source."""
from __future__ import annotations

import json
import logging
import sqlite3

from . import db, llm, progress
from .config import Settings
from .script import siblings

LONG_VIDEO_SECONDS = 600  # beyond this the archive description rarely describes one fragment

log = logging.getLogger(__name__)

PROMPT = """You are a strict fact checker for short factual videos.
For each Short below you get the on-screen text, the list of claimed facts with their source,
and the archive's original description of the footage.

For every fact decide:
- "supported": the quote/source clearly supports it, or it is textbook-level established knowledge
  stated precisely (no rounding that changes meaning, no superlatives that are false).
- "unclear": plausible but you cannot confirm it from the source or it is phrased too loosely.
- "unsupported": wrong, exaggerated, or the URL is not a real page you know.
Also list any factual claim in the on-screen text that is NOT in the facts list as an extra fact
with verdict "unclear" or "unsupported", and check that the on-screen text does not misdescribe
what the footage shows according to the archive description.
When a Short lists "Other Shorts from the same video", add an extra fact with verdict "unclear"
if this Short repeats the main fact or hook of one of them. When the footage is long, the archive
description covers the whole video and not necessarily this fragment: be strict that the text
does not claim the shot shows something the description only mentions elsewhere.

Shorts:
{items}

Answer with only JSON:
[{{"short": <id>, "facts": [{{"claim": "...", "verdict": "supported|unclear|unsupported",
   "note": "short reason, and a correction if wrong"}}]}}]
"""


def _item(conn: sqlite3.Connection, short_id: int) -> str:
    s = conn.execute(
        """SELECT s.id, a.id AS asset_id, a.title, a.description, a.duration, sc.start FROM shorts s
             JOIN scenes sc ON sc.id = s.hero_scene_id JOIN assets a ON a.id = sc.asset_id
            WHERE s.id = ?""", (short_id,)).fetchone()
    blocks = [r[0] for r in conn.execute("SELECT text FROM blocks WHERE short_id=? ORDER BY idx", (short_id,))]
    facts = [dict(r) for r in conn.execute(
        "SELECT claim, source_url, source_quote FROM facts WHERE short_id=?", (short_id,))]
    text = (f"Short {s['id']}:\n  On-screen text: {' / '.join(blocks)}\n"
            f"  Facts: {json.dumps(facts, ensure_ascii=False)}\n"
            f"  Footage title: {s['title']}\n  Archive description: {(s['description'] or '')[:2500]}")
    if (s["duration"] or 0) > LONG_VIDEO_SECONDS:
        text += (f"\n  Footage: a {s['duration'] / 60:.0f}-minute video; the hero shot is from "
                 f"{s['start'] / 60:.1f} min in")
    others = []
    for sib in siblings(conn, s["asset_id"], exclude=short_id):
        texts = [r[0] for r in conn.execute("SELECT text FROM blocks WHERE short_id=? ORDER BY idx", (sib["id"],))]
        others.append(" / ".join(texts))
    if others:
        text += "\n  Other Shorts from the same video: " + " || ".join(others)
    return text


def check(conn: sqlite3.Connection, settings: Settings, short_ids: list[int], batch_size: int = 5) -> None:
    for i in range(0, len(short_ids), batch_size):
        progress.report(i, len(short_ids), "fact check")
        ids = short_ids[i:i + batch_size]
        prompt = PROMPT.format(items="\n\n".join(_item(conn, sid) for sid in ids))
        try:
            results = llm.ask_json(prompt, settings.claude_cmd)
        except llm.LLMError as e:
            log.warning("fact check failed for %s: %s", ids, e)
            continue
        if isinstance(results, dict):
            results = [results]
        for res in results if isinstance(results, list) else []:
            try:
                sid = int(res.get("short"))
            except (TypeError, ValueError, AttributeError):
                log.warning("fact check reply without a valid short id: %s", str(res)[:200])
                continue
            # Only Shorts from this batch; anything not answered stays 'scripted' and is retried.
            if sid in ids:
                apply_result(conn, sid, res.get("facts") or [])
            else:
                log.warning("fact check answered for short %s, which was not in this batch", sid)


def apply_result(conn: sqlite3.Connection, short_id: int, checked: list[dict]) -> None:
    existing = {r["claim"].strip().lower(): r["id"] for r in conn.execute(
        "SELECT id, claim FROM facts WHERE short_id=?", (short_id,))}
    for f in checked:
        claim = (f.get("claim") or "").strip()
        verdict = f.get("verdict") if f.get("verdict") in ("supported", "unclear", "unsupported") else "unclear"
        fid = existing.get(claim.lower())
        if fid:
            conn.execute("UPDATE facts SET verdict=?, note=? WHERE id=?", (verdict, f.get("note"), fid))
        elif claim:
            conn.execute("INSERT INTO facts (short_id, claim, verdict, note) VALUES (?,?,?,?)",
                         (short_id, claim, verdict, f.get("note")))
    # Facts the checker skipped stay unverified, which counts as a flag.
    conn.execute("UPDATE facts SET verdict='unclear', note='not checked' WHERE short_id=? AND verdict IS NULL",
                 (short_id,))
    bad = conn.execute("SELECT COUNT(*) FROM facts WHERE short_id=? AND verdict!='supported'",
                       (short_id,)).fetchone()[0]
    db.set_short(conn, short_id, status="checked", factcheck_ok=int(bad == 0))
