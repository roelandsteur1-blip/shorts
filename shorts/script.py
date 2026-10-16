"""Write scripts around hero shots (batched, to spare the Claude subscription limit)."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3

from . import llm, progress
from .config import Channel, Settings

log = logging.getLogger(__name__)

MAX_WORDS = 8
WORDS_PER_SECOND = 3.0
MIN_BLOCK_SECONDS = 1.6

PROMPT = """You write on-screen text for YouTube Shorts on the channel "{channel_name}" ({niche}).
There is NO voice-over: the text on screen is the whole story, read silently over real archive footage ({archives}).
Language: {language_name}.

Format: "{format_title}"
{format_brief}

For EACH hero shot below, write one Short built around that shot. The hero shot is the first thing
the viewer sees; the first text block is the hook and must make sense on top of that image.

Rules:
- {min_blocks} to {max_blocks} text blocks, each at most {max_words} words. Plain words, no emoji,
  no hashtags. Wrap one or two key words per block in *asterisks* to highlight them.
- Block 1 is the hook: a question, a surprising claim or "you're looking at...". Never "Did you know".
- Every factual claim must be true and must be listed in "facts" with a source. Prefer the archive
  description given below (source "archive" + the exact supporting quote). If a fact is general
  knowledge not in that text, give a specific, real {url_hint} URL you are
  sure exists, and the quote it supports; do not invent URLs. When in doubt, leave the fact out.
- For blocks after the first, "visual" describes in 3-8 plain words what footage should show
  (e.g. "Saturn rings close up", "astronaut floating inside station"). Block 1 visual is "hero".
- Do not repeat these topics we already covered: {past_topics}
- Title: max 70 characters, curiosity-driven, no clickbait lies, no hashtags.
- Description: 1-2 sentences in plain English expanding on the Short.
- Tags: 5-10 search tags.

Hero shots:
{heroes}

Answer with only a JSON array, one object per hero shot, in the same order:
[{{"hero": <hero number>, "topic": "...", "blocks": [{{"text": "...", "visual": "..."}}],
  "facts": [{{"claim": "...", "source": "archive" or "https://...", "quote": "..."}}],
  "title": "...", "description": "...", "tags": ["..."]}}]
"""

LANGS = {"en": "English", "nl": "Dutch", "de": "German", "es": "Spanish", "fr": "French"}


def hero_context(conn: sqlite3.Connection, scene_id: int) -> dict:
    r = conn.execute(
        """SELECT sc.id AS scene_id, sc.start, sc.end, a.id AS asset_id, a.title, a.description,
                  a.keywords, a.date_created, a.source_url, a.credit, a.source
             FROM scenes sc JOIN assets a ON a.id = sc.asset_id WHERE sc.id = ?""",
        (scene_id,),
    ).fetchone()
    return dict(r) | {"archive": archive_name(r["source"])}


def archive_name(source: str) -> str:
    from .sources import label
    return "NASA" if source == "nasa" else label(source)


def url_hint(channel: Channel) -> str:
    """Where Claude should look for sources beyond the footage description."""
    if not channel.categories or "space" in channel.categories:
        return "nasa.gov / esa.int / jpl.nasa.gov"
    return "reputable (museum, national archive, university, government or encyclopedia)"


def build_prompt(channel: Channel, fmt: dict, heroes: list[dict], past_topics: list[str]) -> str:
    max_blocks = max(3, math.floor(channel.max_seconds / 2.6))
    min_blocks = max(3, math.ceil(channel.min_seconds / 3.0))
    hero_text = "\n\n".join(
        f"Hero {i + 1}:\n"
        f"  Archive: {h['archive']}\n"
        f"  Title: {h['title']}\n"
        f"  Date: {(h.get('date_created') or '')[:10]}\n"
        f"  Keywords: {h.get('keywords') or ''}\n"
        f"  Description: {(h.get('description') or '').strip()[:2500]}\n"
        f"  The hero shot is {h['end'] - h['start']:.1f}s taken from {h['start']:.0f}s into this video."
        for i, h in enumerate(heroes)
    )
    return PROMPT.format(
        channel_name=channel.name, niche=channel.niche,
        language_name=LANGS.get(channel.language, channel.language),
        format_title=fmt["title"], format_brief=fmt["brief"].strip(),
        min_blocks=min_blocks, max_blocks=max_blocks, max_words=MAX_WORDS,
        past_topics="; ".join(past_topics[-80:]) or "(none yet)",
        heroes=hero_text, url_hint=url_hint(channel),
        archives=", ".join(dict.fromkeys(h.get("archive", "NASA") for h in heroes)),
    )


def split_block(text: str, max_words: int = MAX_WORDS) -> list[str]:
    words = text.split()
    if len(words) <= max_words:
        return [text.strip()]
    n = math.ceil(len(words) / max_words)
    size = math.ceil(len(words) / n)
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)]


def block_seconds(text: str) -> float:
    words = len(re.sub(r"[*]", "", text).split())
    return round(max(MIN_BLOCK_SECONDS, words / WORDS_PER_SECOND + 0.5), 2)


def normalise_blocks(raw_blocks: list[dict], channel: Channel) -> list[dict]:
    blocks = []
    for i, b in enumerate(raw_blocks):
        visual = "hero" if i == 0 else (b.get("visual") or "").strip()
        for j, part in enumerate(split_block(b.get("text", ""))):
            if part:
                blocks.append({"text": part, "visual": visual if j == 0 else (visual or "hero"),
                               "seconds": block_seconds(part)})
    total = sum(b["seconds"] for b in blocks)
    # Stretch short scripts to the minimum length; never squeeze reading time below the rule.
    if blocks and total < channel.min_seconds:
        factor = channel.min_seconds / total
        for b in blocks:
            b["seconds"] = round(b["seconds"] * factor, 2)
    # Last block lingers a bit so the ending lands (and the loop feels natural).
    if blocks:
        blocks[-1]["seconds"] = round(blocks[-1]["seconds"] + 0.6, 2)
    return blocks


def save_script(conn: sqlite3.Connection, channel: Channel, fmt_id: str, hero: dict, data: dict) -> int:
    blocks = normalise_blocks(data.get("blocks") or [], channel)
    if len(blocks) < 3:
        raise ValueError("script too short")
    tags = [t.strip().lstrip("#") for t in data.get("tags") or [] if t.strip()]
    cur = conn.execute(
        """INSERT INTO shorts (channel, format_id, hero_scene_id, topic, hook, title, description,
                               tags, status, duration)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scripted', ?)""",
        (channel.id, fmt_id, hero["scene_id"], data.get("topic"), blocks[0]["text"],
         (data.get("title") or "")[:100], data.get("description") or "", json.dumps(tags),
         sum(b["seconds"] for b in blocks)),
    )
    short_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO blocks (short_id, idx, text, seconds, visual, scene_id) VALUES (?,?,?,?,?,?)",
        [(short_id, i, b["text"], b["seconds"], b["visual"], hero["scene_id"] if i == 0 else None)
         for i, b in enumerate(blocks)],
    )
    conn.executemany(
        "INSERT INTO facts (short_id, claim, source_url, source_quote) VALUES (?,?,?,?)",
        [(short_id, f.get("claim", ""),
          hero["source_url"] if f.get("source") in (None, "", "nasa", "archive") else f["source"],
          f.get("quote")) for f in data.get("facts") or []],
    )
    conn.commit()
    return short_id


def past_topics(conn: sqlite3.Connection, channel: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT topic FROM shorts WHERE channel=? AND topic IS NOT NULL ORDER BY id", (channel,))]


def write_scripts(conn: sqlite3.Connection, settings: Settings, channel: Channel, fmt_id: str,
                  hero_scene_ids: list[int], batch_size: int = 5) -> list[int]:
    fmt = channel.format(fmt_id)
    created = []
    for i in range(0, len(hero_scene_ids), batch_size):
        progress.report(i, len(hero_scene_ids), "script")
        heroes = [hero_context(conn, sid) for sid in hero_scene_ids[i:i + batch_size]]
        prompt = build_prompt(channel, fmt, heroes, past_topics(conn, channel.id))
        replies = llm.ask_json(prompt, settings.claude_cmd)
        if isinstance(replies, dict):
            replies = [replies]
        done_heroes: set[int] = set()
        for reply in replies if isinstance(replies, list) else []:
            try:
                n = int(reply.get("hero"))
                # Only hero numbers from this batch, each once: a missing or wrong number must never
                # attach a script to the wrong footage.
                if not 1 <= n <= len(heroes) or n in done_heroes:
                    raise ValueError(f"unexpected hero number {reply.get('hero')!r}")
                created.append(save_script(conn, channel, fmt_id, heroes[n - 1], reply))
                done_heroes.add(n)
            except (ValueError, TypeError, AttributeError) as e:
                log.warning("skipping a script: %s", e)
    return created


def edit_blocks(conn: sqlite3.Connection, channel: Channel, short_id: int, texts: dict[int, str]) -> bool:
    """Change on-screen text by hand (review page). Reading time follows the new text, the Short
    keeps its minimum length, and the footage stays. Returns True when something changed; the
    caller re-renders."""
    blocks = [dict(r) for r in conn.execute("SELECT * FROM blocks WHERE short_id=? ORDER BY idx", (short_id,))]
    changed = False
    for b in blocks:
        new = " ".join((texts.get(b["idx"]) or "").split())
        if new and new != b["text"]:
            b["text"], changed = new, True
            b["seconds"] = block_seconds(new) + (0.6 if b is blocks[-1] else 0)
    if not changed:
        return False
    total = sum(b["seconds"] for b in blocks)
    if total < channel.min_seconds:
        for b in blocks:
            b["seconds"] = round(b["seconds"] * channel.min_seconds / total, 2)
    conn.executemany("UPDATE blocks SET text=?, seconds=? WHERE id=?",
                     [(b["text"], round(b["seconds"], 2), b["id"]) for b in blocks])
    conn.execute("""UPDATE shorts SET hook=?, duration=?, edited=1, updated_at=datetime('now') WHERE id=?""",
                 (blocks[0]["text"], sum(b["seconds"] for b in blocks), short_id))
    conn.commit()
    return True
