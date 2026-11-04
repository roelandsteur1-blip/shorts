"""Video types per channel. 'classic' is the original Short (hero shot, script, captions, voice);
the others come from the meta maker (meta/): text card, quiz, ranking, or a mix of the three
(research/youtube-meta.md). A meta Short lands in the same `shorts` table as a classic one, as
'rendered', so Review, upload, schedule and stats work the same. Its video can't be re-rendered
from edited text: reject it and make a new one instead."""
from __future__ import annotations

import json
import logging
import sqlite3

from . import db, llm, progress
from .config import Channel, Settings

log = logging.getLogger(__name__)

TYPES = {
    "classic": "Classic: hero shot with script, captions and voice (the original)",
    "textcard": "Text card: one surprising fact as a post, archive clip below (6-15 s loop)",
    "quiz": "Quiz: 'Can you name this?' in 3 levels (~20 s)",
    "ranking": "Ranking: 4-5 things ranked by one number (~18 s)",
    "mix": "Mix: text card, quiz and ranking in turn",
}
META = ("textcard", "quiz", "ranking")
# Topic sets of the meta maker; a channel's is guessed from its id and name unless set.
NICHE_LABELS = {
    "space": "Space", "history": "History", "body": "Human body", "warplane": "Warplanes",
    "ocean": "Ocean", "planet": "Wild animals", "weather": "Weather", "earthquake": "Earth & disasters",
    "retrotech": "Retro tech", "oldcity": "Old cities",
}
_GUESS = [("earthquake", "earthquake"), ("oldcity", "oldcit"), ("oldcity", "city"), ("retrotech", "retro"),
          ("warplane", "warplane"), ("planet", "planet"), ("warplane", "plane"), ("weather", "weather"),
          ("ocean", "ocean"), ("planet", "wild"), ("body", "body"), ("history", "histor"), ("space", "space")]


def niche_for(channel: Channel) -> str | None:
    return guess_niche(channel.id, channel.name, channel.meta_niche)


def guess_niche(cid: str | None, name: str | None, meta_niche: str | None = None) -> str | None:
    """The meta maker's topic set: the one picked, else guessed from the channel's id and name."""
    if meta_niche in NICHE_LABELS:
        return meta_niche
    text = f"{cid or ''} {name or ''}".lower()
    return next((niche for niche, word in _GUESS if word in text), None)


def available(niche: str | None) -> list[str]:
    """Meta types that work for a topic set (quiz and ranking need a theme in meta/quiz.py)."""
    if not niche:
        return []
    from meta.quiz import THEMES
    return ["textcard"] + (["quiz", "ranking"] if niche in THEMES else [])


def is_meta(channel: Channel) -> bool:
    return (channel.video_type or "classic") != "classic"


def _next_type(conn: sqlite3.Connection, channel: Channel, choices: list[str]) -> str:
    if channel.video_type in choices:
        return channel.video_type
    if channel.video_type == "mix":
        last = conn.execute("SELECT format_id FROM shorts WHERE channel=? AND format_id LIKE 'meta_%' "
                            "ORDER BY id DESC LIMIT 1", (channel.id,)).fetchone()
        prev = last[0][5:] if last else None
        return choices[(choices.index(prev) + 1) % len(choices)] if prev in choices else choices[0]
    return "textcard"  # quiz/ranking picked for a topic without a theme


def make(conn: sqlite3.Connection, settings: Settings, channel: Channel, count: int) -> int:
    """Make `count` meta Shorts for the channel; returns how many were made."""
    niche = niche_for(channel)
    choices = available(niche)
    if not choices:
        log.warning("%s: no topic set for its video type: pick one on the channel page", channel.id)
        return 0
    made = attempts = 0
    while made < count and attempts < count * 2 and not llm.limit_reached():
        attempts += 1
        kind = _next_type(conn, channel, choices)
        recs = _try(conn, settings, channel, niche, kind)
        if not recs and channel.video_type == "mix" and kind != "textcard":
            recs = _try(conn, settings, channel, niche, "textcard")  # too little footage: a text card instead
        for rec in recs:
            sid = save(conn, channel, rec)
            made += 1
            log.info("%s: %s Short %d: %s", channel.id, rec["kind"], sid, rec["title"])
        progress.report(made, count, "meta")
    if made < count:
        log.warning("%s: made %d of %d %s Shorts (not enough fitting footage this run)", channel.id, made,
                    count, channel.video_type)
    return made


def _try(conn: sqlite3.Connection, settings: Settings, channel: Channel, niche: str, kind: str) -> list[dict]:
    try:
        return _one(conn, settings, channel, niche, kind)
    except Exception:  # noqa: BLE001 - one failed Short must not stop the batch
        log.exception("%s: %s failed", channel.id, kind)
        return []


def _one(conn: sqlite3.Connection, settings: Settings, channel: Channel, niche: str, kind: str) -> list[dict]:
    lib = {
        "settings": settings, "conn": conn, "llm": llm,
        "out_dir": settings.render_dir / "meta" / channel.id,
        "used": db.used_scene_ids(conn, channel.id),
        "avoid": [r[0] for r in conn.execute(
            "SELECT title FROM shorts WHERE channel=? AND title IS NOT NULL ORDER BY id DESC LIMIT 40", (channel.id,))],
        "name": channel.name, "handle": "@" + channel.id,
    }
    if kind == "textcard":
        from meta import textcard
        recs = textcard.make(niche, 1, [], lib=lib)
    elif kind == "quiz":
        from meta import quiz
        recs = quiz.make(niche, 1, lib=lib)
    else:
        from meta import ranking
        recs = ranking.make(niche, 1, lib=lib)
    return [dict(r, kind=kind) for r in recs]


def describe(rec: dict) -> tuple[str, list[tuple[str, float]]]:
    """(description, [(block text, seconds)]) for a made Short; one block per clip."""
    kind = rec["kind"]
    if kind == "textcard":
        return rec["text"], [(rec["text"], rec["seconds"])]
    if kind == "quiz":
        each = rec["seconds"] / max(1, len(rec["rounds"]))
        return ("How many could you name? Answers are in the video. Comment your score!",
                [(f"{r['answer']}: {r.get('fact') or ''}".strip(": "), each) for r in rec["rounds"]])
    each = rec["seconds"] / max(1, len(rec["items"]))
    lines = [f"{it['rank']}. {it['name']} ({it['value']})" for it in rec["items"]]
    return f"{rec.get('heading') or rec['title']}\n" + "\n".join(lines), [(x, each) for x in lines]


def save(conn: sqlite3.Connection, channel: Channel, rec: dict) -> int:
    description, blocks = describe(rec)
    cur = conn.execute(
        """INSERT INTO shorts (channel, format_id, hero_scene_id, topic, hook, title, description, tags,
                               status, video_path, duration, music)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'rendered', ?, ?, ?)""",
        (channel.id, f"meta_{rec['kind']}", rec["scene_ids"][0], rec["title"], rec["title"], rec["title"],
         description, json.dumps([]), rec["file"], rec["seconds"], rec.get("music")))
    sid = cur.lastrowid
    for i, ((text, secs), scene) in enumerate(zip(blocks, rec["scene_ids"])):
        conn.execute("INSERT INTO blocks (short_id, idx, text, seconds, scene_id) VALUES (?, ?, ?, ?, ?)",
                     (sid, i, text, secs, scene))
    conn.commit()
    return sid
