"""Which videos fit which channel. Every channel's niche decides for itself, so channels and footage
categories can grow without one getting the other's footage: categories only say where restock
searches, a channel's niche says what it may use.

One Claude call sorts a batch of videos (title, subjects, description) against the niches of every
channel that hasn't judged them yet, so the number of calls follows the footage coming in, not the
number of channels. A new channel, or a changed niche, sorts the library once for that channel.
Answers are stored in channel_fit; corrections by hand (Library page) stick whatever the niche."""
from __future__ import annotations

import logging
import sqlite3

from . import llm, progress
from .config import Channel, Settings, all_channels

log = logging.getLogger(__name__)

BATCH = 40  # videos per call
ANY_NICHE = "*"

PROMPT = """You sort public-domain archive videos for YouTube Shorts channels. Each channel is only about its niche.

Channels (id: niche):
{channels}

Videos:
{items}

For each video, list the channels that a Short built on this footage would clearly fit. A loose
link is not enough: a flood newsreel is not medicine, a steel mill is not biology, a warship is not
weather. When the title, subjects and description don't say what the footage shows, list none.
Answer with only JSON: [{{"n": <video number>, "channels": ["id", ...]}}]
"""

# Videos that can still be used: not flagged, not claimed, not cleaned up, with usable scenes.
USABLE = """a.needs_check = 0 AND a.claimed IS NULL AND a.status NOT IN ('cleaned', 'duplicate', 'rejected')
            AND EXISTS (SELECT 1 FROM scenes s WHERE s.asset_id = a.id AND s.rejected = 0)"""


def condition(channel: Channel, alias: str = "sc") -> tuple[str, list]:
    """SQL condition (and its parameters): scenes from videos that fit this channel."""
    return (f"""{alias}.asset_id IN (SELECT asset_id FROM channel_fit WHERE channel = ? AND fits = 1
                                     AND niche IN (?, '{ANY_NICHE}'))""", [channel.id, channel.niche])


def rejected(conn: sqlite3.Connection, channel: Channel) -> set[int]:
    """Videos judged not to fit this channel's current niche."""
    return {r[0] for r in conn.execute(
        f"SELECT asset_id FROM channel_fit WHERE channel=? AND fits=0 AND niche IN (?, '{ANY_NICHE}')",
        (channel.id, channel.niche))}


def allowed_scenes(conn: sqlite3.Connection, channel: Channel) -> set[int]:
    cond, args = condition(channel)
    return {r[0] for r in conn.execute(f"SELECT sc.id FROM scenes sc WHERE {cond}", args)}


def mark(conn: sqlite3.Connection, channel: Channel, asset_id: int, fits: bool, reason: str = "",
         method: str = "manual", commit: bool = True) -> None:
    """Record one answer. By hand it sticks for every niche; the script writer's skip and Claude's
    sort hold for the niche they were made against."""
    niche = ANY_NICHE if method == "manual" else channel.niche
    if method != "manual" and conn.execute(
            "SELECT 1 FROM channel_fit WHERE channel=? AND asset_id=? AND method='manual'",
            (channel.id, asset_id)).fetchone():
        return  # a correction by hand wins
    conn.execute("""INSERT OR REPLACE INTO channel_fit (channel, asset_id, fits, niche, reason, method)
                    VALUES (?,?,?,?,?,?)""", (channel.id, asset_id, int(fits), niche, reason[:200], method))
    if commit:
        conn.commit()


def pending(conn: sqlite3.Connection, channels: list[Channel]) -> dict[int, list[Channel]]:
    """Usable videos, each with the channels that haven't judged it against their current niche."""
    out: dict[int, list[Channel]] = {}
    assets = [r[0] for r in conn.execute(f"SELECT a.id FROM assets a WHERE {USABLE} ORDER BY a.id")]
    for ch in channels:
        done = {r[0] for r in conn.execute(
            f"SELECT asset_id FROM channel_fit WHERE channel=? AND niche IN (?, '{ANY_NICHE}')",
            (ch.id, ch.niche))}
        for aid in assets:
            if aid not in done:
                out.setdefault(aid, []).append(ch)
    return out


def sort_pending(conn: sqlite3.Connection, settings: Settings, channels: list[Channel] | None = None,
                 extra: list[Channel] = ()) -> int:
    """Sort every usable video that a channel hasn't judged yet. channels: default every channel;
    extra: these too (the ones a run is about to make Shorts for, as loaded for it). Returns
    videos sorted. A failed call leaves the rest for the next run."""
    channels = channels if channels is not None else all_channels(settings.config_dir)
    channels = list({c.id: c for c in [*channels, *extra]}.values())
    todo = pending(conn, channels)
    if not todo:
        return 0
    ids = list(todo)
    log.info("sorting %d videos for %s", len(ids), ", ".join(sorted({c.id for v in todo.values() for c in v})))
    done = 0
    for i in range(0, len(ids), BATCH):
        progress.report(i, len(ids), "sort footage per channel")
        batch = ids[i:i + BATCH]
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT id, title, keywords, description FROM assets WHERE id IN ({','.join('?' * len(batch))})", batch)}
        asked = {c.id: c for aid in batch for c in todo[aid]}
        items = "\n".join(
            f"{n + 1}. {rows[aid]['title'] or ''} | subjects: {(rows[aid]['keywords'] or '')[:200]} | "
            f"{(rows[aid]['description'] or '')[:300]}" for n, aid in enumerate(batch))
        prompt = PROMPT.format(channels="\n".join(f"{c.id}: {c.niche}" for c in asked.values()), items=items)
        try:
            reply = llm.ask_json(prompt, settings.claude_cmd, conn=conn, stage="claude sort channels")
        except llm.LLMError as e:
            log.warning("could not sort footage per channel (%s); %d videos wait for the next run",
                        e, len(ids) - i)
            break
        answers: dict[int, set[str]] = {}
        for r in reply if isinstance(reply, list) else []:
            try:
                n = int(r.get("n"))
            except (TypeError, ValueError, AttributeError):
                continue
            if 1 <= n <= len(batch) and isinstance(r.get("channels"), list):
                answers[batch[n - 1]] = {str(c) for c in r["channels"]}
        for aid, chans in answers.items():
            for ch in todo[aid]:
                mark(conn, ch, aid, ch.id in chans, method="claude", commit=False)
            done += 1
        conn.commit()
    progress.report(len(ids), len(ids), "sort footage per channel")
    return done
