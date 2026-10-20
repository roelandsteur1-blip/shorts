"""Write scripts around hero shots (batched, to spare the Claude subscription limit)."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3

from . import formats as fmtlib, inspiration, learn, llm, looks, progress, tags as taglayers
from .config import Channel, Settings
from .music import MOODS
from .voice import channel_voice

log = logging.getLogger(__name__)

MAX_WORDS = 6           # per text block; blocks also never wrap past 2 lines (looks.MAX_LINES)
WORDS_PER_SECOND = 3.0
MIN_BLOCK_SECONDS = 1.6
END_LINGER = 0.3        # extra seconds on the last block: land the payoff, then cut (no long fade)

PROMPT = """You write on-screen text for YouTube Shorts on the channel "{channel_name}" ({niche}).
{voice_line} ({archives}).
Language: {language_name}.

{format_section}

For EACH hero shot below, write one Short built around that shot. The hero shot is the first thing
the viewer sees; the first text block is the hook and must make sense on top of that image.

Every Short is a tiny story that teaches ONE specific thing, never a list of loose facts over
footage (YouTube does not recommend or pay for templated, mass-produced Shorts):
- Block 1 is the hook: open on the most surprising true claim or a sharp question about what is on
  screen, with honest emotion (awe, danger, strangeness). No date or background first. Never
  "Did you know", "You won't believe", or anything the Short doesn't deliver.
- Block 2 hints at the payoff so the viewer wants to stay ("but that's not the strange part").
- The middle blocks build: link them with "but" / "so" / "then", each adding something new.
- The LAST block is the payoff: the most surprising fact or the answer to the hook. Nothing after
  it: no summary, no "comment below", no "follow for more". When it fits naturally, word it so it
  runs straight on into the hook when the Short replays (a loop: "...and that is why" -> hook),
  and give it the visual "hero" so the picture returns to the opening shot. Never force it.
- Never write as a person or an expert ("As an engineer...", "I").

Rules:
- {min_blocks} to {max_blocks} text blocks, each at most {max_words} words. Plain words, no emoji,
  no hashtags. Wrap the one key word or number of a block in *asterisks* to highlight it (at most
  two words per block).
- Every factual claim must be true and must be listed in "facts" with a source. Prefer the archive
  description given below (source "archive" + the exact supporting quote). If a fact is general
  knowledge not in that text, give a specific, real {url_hint} URL you are
  sure exists, and the quote it supports; do not invent URLs. When in doubt, leave the fact out.
- For blocks after the first, "visual" describes in 3-8 plain words what footage should show,
  preferring one clear subject close up (e.g. "Saturn rings close up", "astronaut floating inside
  station"). Block 1 visual is "hero".
- Title: max 70 characters. Name the subject plainly so people searching for it find it (e.g.
  "Saturn's rings are disappearing"), make it match the video, no clickbait lies, no hashtags, and
  don't start every title the same way.
{screen_title_rule}- Description: 1-2 sentences in plain English expanding on the Short.
- Tags: exactly 3 search tags about this exact Short, what someone would type to find it (for a
  Short about a bicep workout: "bicep curl", "bicep workout", "arm workout"). Not the channel's
  niche in general and not generic words like "viral" or "shorts" (those are added separately).
- Mood: the background music that fits this Short, one of: {moods}.

Niche check first: this channel is only about {niche}. If a hero video (title, description, keywords)
is not clearly about that, do NOT write a Short for it; answer {{"hero": <hero number>, "skip": "<why, a few words>"}}
for it instead. A loose link ("the steel in this mill is also in your blood") is not enough.

Answer with only a JSON array, one object per hero shot, in the same order:
[{{"hero": <hero number>, {format_field}"topic": "...", "blocks": [{{"text": "...", "visual": "..."}}],
  "facts": [{{"claim": "...", "source": "archive" or "https://...", "quote": "..."}}],
  "title": "...", {screen_title_field}"description": "...", "tags": ["..."], "mood": "..."}}]

{lessons}Topics we already covered (do not repeat them): {past_topics}
{other_topics}
Hero shots:
{heroes}
"""

SCREEN_TITLE_RULE = """- Screen title: shown at the top of the video, so a viewer knows at a glance what this Short is
  about (many watch muted). 2 to 6 words, at most 32 characters, no question mark needed. Wrap the 1
  or 2 key words in *asterisks*. It names the subject, it does not repeat the hook (e.g. "Saturn's
  *vanishing* rings", "The *loudest* sound ever").
"""
SCREEN_TITLE_CHARS = 40

LANGS = {"en": "English", "nl": "Dutch", "de": "German", "es": "Spanish", "fr": "French"}


def hero_context(conn: sqlite3.Connection, scene_id: int) -> dict:
    r = conn.execute(
        """SELECT sc.id AS scene_id, sc.start, sc.end, a.id AS asset_id, a.title, a.description,
                  a.keywords, a.date_created, a.source_url, a.credit, a.source
             FROM scenes sc JOIN assets a ON a.id = sc.asset_id WHERE sc.id = ?""",
        (scene_id,),
    ).fetchone()
    return dict(r) | {"archive": archive_name(r["source"]), "siblings": siblings(conn, r["asset_id"])}


def siblings(conn: sqlite3.Connection, asset_id: int, exclude: int | None = None) -> list[sqlite3.Row]:
    """Earlier non-rejected Shorts (any channel) whose hero shot comes from this video."""
    return conn.execute(
        """SELECT s.id, s.topic, s.hook, sc.start FROM shorts s JOIN scenes sc ON sc.id = s.hero_scene_id
            WHERE sc.asset_id = ? AND s.status != 'rejected' AND s.id != ? ORDER BY s.id""",
        (asset_id, exclude or -1)).fetchall()


def sibling_note(h: dict) -> str:
    if not h.get("siblings"):
        return ""
    done = "; ".join(f"\"{s['topic']}\" (hook: {s['hook']})" for s in h["siblings"])
    return (f"\n  This video already gave other Shorts: {done}. Pick a different fact and angle, "
            "about what THIS part of the video shows; do not repeat their facts or hook.")


def archive_name(source: str) -> str:
    from .sources import label
    return "NASA" if source == "nasa" else label(source)


def url_hint(channel: Channel) -> str:
    """Where Claude should look for sources beyond the footage description."""
    if not channel.categories or "space" in channel.categories:
        return "nasa.gov / esa.int / jpl.nasa.gov"
    return "reputable (museum, national archive, university, government or encyclopedia)"


MIN_RANKED = 6     # Shorts with stats before the best and worst ones go into the prompt
SHOWN = 3          # best and worst Shorts shown
MAX_REJECTS = 10   # recent rejected Shorts shown
NOT_SCRIPT = ("bad footage", "rights doubt")  # reject reasons that say nothing about the writing


def winners_note(conn: sqlite3.Connection, channel: str) -> str:
    """The channel's best and worst Shorts by engaged views at the same age (longest age with enough
    Shorts). Engaged, not plain views: since 2025 a view counts the moment a Short starts playing."""
    for days in (7, 3, 1):
        rows = conn.execute(
            """SELECT s.hook, s.title, a.views, a.engaged_views FROM stats_age a JOIN shorts s ON s.id = a.short_id
                WHERE s.channel = ? AND a.days = ? AND NOT s.stats_hidden
                ORDER BY a.engaged_views DESC, a.views DESC""", (channel, days)).fetchall()
        if len(rows) >= MIN_RANKED:
            break
    else:
        return ""
    n = min(SHOWN, len(rows) // 2)
    line = lambda r: (f'- {r["engaged_views"] or 0:,} engaged views ({r["views"] or 0:,} views). '  # noqa: E731
                      f'Hook: "{r["hook"]}" Title: "{r["title"]}"')
    return (f"What worked on this channel so far (engaged views in a Short's first {days} day{'s' if days > 1 else ''}, "
            "people who kept watching instead of swiping away). "
            "Learn from the pattern in hooks and titles, don't copy them:\nBest:\n"
            + "\n".join(line(r) for r in rows[:n]) + "\nWorst:\n" + "\n".join(line(r) for r in rows[-n:]) + "\n\n")


def rejects_note(conn: sqlite3.Connection, channel: str) -> str:
    """Recent Shorts the reviewer rejected, with the reason typed in review."""
    rows = [r for r in conn.execute(
        """SELECT hook, title, reject_reason FROM shorts WHERE channel = ? AND status = 'rejected'
              AND reject_reason IS NOT NULL ORDER BY updated_at DESC, id DESC LIMIT ?""",
        (channel, MAX_REJECTS * 2)) if not r["reject_reason"].startswith(NOT_SCRIPT)][:MAX_REJECTS]
    if not rows:
        return ""
    return ("The reviewer rejected these recent Shorts on this channel. Avoid the same mistakes:\n"
            + "\n".join(f'- Hook: "{r["hook"]}" Title: "{r["title"]}" Reason: {r["reject_reason"][:200]}'
                         for r in rows) + "\n\n")


FORMAT_MIN_RATED = 3  # Shorts with numbers a format needs before its result is shown


def format_section(fmts: list[dict], used: dict[str, int] | None = None,
                   results: dict | None = None) -> tuple[str, str]:
    """(prompt text, extra JSON field) for one fixed format or a choice of several.
    results: shorts/learn.py group scores per format: how each did on this channel."""
    if len(fmts) == 1:
        return f'Format: "{fmts[0]["title"]}"\n{fmts[0]["brief"].strip()}', ""
    from .learn import percent
    used = used or {}
    rated = {k: v for k, v in (results or {}).items() if v["shorts"] >= FORMAT_MIN_RATED}

    def note(f):
        r = rated.get(f["id"])
        res = f', does {percent(r["score"])} vs the channel\'s usual over {r["shorts"]} Shorts' if r else ""
        return f'used {used.get(f["id"], 0)}x lately{res}'
    lines = "\n".join(f'- "{f["id"]}" ({f["title"]}, {note(f)}): {" ".join(f["brief"].split())}' for f in fmts)
    rule = ("preferring formats used least lately" if not rated else
            "giving the formats that do better more Shorts, but every format at least one in five so "
            "it keeps being tested, and among equals the one used least lately")
    return ("Formats (series) of this channel. For each hero shot pick the one that fits it best; over the\n"
            f"batch use different ones, {rule}:\n" + lines,
            '"format": "<format id>", ')


def format_results(conn: sqlite3.Connection, channel: str) -> dict:
    from . import learn
    return learn.group_scores(learn.short_scores(conn, channel), lambda s: s["format_id"])


def build_prompt(channel: Channel, fmt: dict | list[dict], heroes: list[dict], past_topics: list[str],
                 lessons: str = "", other_topics: list[str] | None = None,
                 format_use: dict[str, int] | None = None, format_results: dict | None = None) -> str:
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
        + sibling_note(h)
        for i, h in enumerate(heroes)
    )
    return PROMPT.format(
        channel_name=channel.name, niche=channel.niche,
        language_name=LANGS.get(channel.language, channel.language),
        format_section=(sections := format_section(fmt if isinstance(fmt, list) else [fmt], format_use, format_results))[0],
        format_field=sections[1],
        other_topics=(f"Topics our other channels covered (pick a different angle or fact): "
                      f"{'; '.join(other_topics[-60:])}\n" if other_topics else ""),
        min_blocks=min_blocks, max_blocks=max_blocks, max_words=MAX_WORDS,
        past_topics="; ".join(past_topics[-80:]) or "(none yet)",
        heroes=hero_text, url_hint=url_hint(channel), moods=", ".join(MOODS), lessons=lessons,
        voice_line=("A narrator voice reads every text block out loud, and the same text is on screen: write "
                    "lines that sound natural spoken and read well, over real archive footage"
                    if channel_voice(channel) else
                    "There is NO voice-over: the text on screen is the whole story, read silently over real archive footage"),
        archives=", ".join(dict.fromkeys(h.get("archive", "NASA") for h in heroes)),
        screen_title_rule=SCREEN_TITLE_RULE if looks.title_bar(channel.style) != "off" else "",
        screen_title_field='"screen_title": "...", ' if looks.title_bar(channel.style) != "off" else "",
    )


def split_block(text: str, max_words: int = MAX_WORDS, style: dict | None = None) -> list[str]:
    """Split a block into parts of at most max_words that each fit in looks.MAX_LINES lines in the
    channel's caption style (balanced parts, so no one-word leftovers)."""
    words = text.split()
    if not words:
        return []
    fits = (lambda ws: len(ws) <= max_words and looks.line_count(" ".join(ws), style) <= looks.MAX_LINES) \
        if style is not None else (lambda ws: len(ws) <= max_words)
    for n in range(math.ceil(len(words) / max_words), len(words) + 1):
        size = math.ceil(len(words) / n)
        parts = [words[i:i + size] for i in range(0, len(words), size)]
        if all(fits(p) for p in parts):
            return [" ".join(p) for p in parts]
    return words


def block_seconds(text: str) -> float:
    words = len(re.sub(r"[*]", "", text).split())
    return round(max(MIN_BLOCK_SECONDS, words / WORDS_PER_SECOND + 0.5), 2)


def normalise_blocks(raw_blocks: list[dict], channel: Channel) -> list[dict]:
    blocks = []
    for i, b in enumerate(raw_blocks):
        visual = "hero" if i == 0 else (b.get("visual") or "").strip()
        for j, part in enumerate(split_block(b.get("text", ""), style=looks.resolve(channel.style))):
            if part:
                blocks.append({"text": part, "visual": visual if j == 0 else (visual or "hero"),
                               "seconds": block_seconds(part)})
    total = sum(b["seconds"] for b in blocks)
    # Stretch short scripts to the minimum length; never squeeze reading time below the rule.
    if blocks and total < channel.min_seconds:
        factor = channel.min_seconds / total
        for b in blocks:
            b["seconds"] = round(b["seconds"] * factor, 2)
    # The last block lingers a moment so the payoff lands, then the Short ends.
    if blocks:
        blocks[-1]["seconds"] = round(blocks[-1]["seconds"] + END_LINGER, 2)
    return blocks


def save_script(conn: sqlite3.Connection, channel: Channel, fmt_id: str | list[str], hero: dict, data: dict) -> int:
    """fmt_id: the format, or the ids Claude could choose from (its "format" field picks one)."""
    if isinstance(fmt_id, list):
        fmt_id = data.get("format") if data.get("format") in fmt_id else fmt_id[0]
    blocks = normalise_blocks(data.get("blocks") or [], channel)
    if len(blocks) < 3:
        raise ValueError("script too short")
    tags = taglayers.pick(data.get("tags") or [])
    cur = conn.execute(
        """INSERT INTO shorts (channel, format_id, hero_scene_id, topic, hook, title, description,
                               tags, status, duration, mood, screen_title)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scripted', ?, ?, ?)""",
        (channel.id, fmt_id, hero["scene_id"], data.get("topic"), blocks[0]["text"],
         (data.get("title") or "")[:100], data.get("description") or "", json.dumps(tags),
         sum(b["seconds"] for b in blocks),
         data.get("mood") if data.get("mood") in MOODS else None,
         clean_screen_title(data.get("screen_title")) if looks.title_bar(channel.style) != "off" else None),
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
    from . import voice
    voice.assign(conn, channel, short_id)
    return short_id


def clean_screen_title(text) -> str | None:
    """One line of plain words (the bar wraps it to 2 lines itself); None when empty."""
    text = " ".join(str(text or "").replace("\n", " ").split()).strip()
    if not text:
        return None
    if len(text.replace("*", "")) > SCREEN_TITLE_CHARS:  # cut at a word, keep highlight marks paired
        words, out = text.split(), []
        for w in words:
            if len(" ".join(out + [w]).replace("*", "")) > SCREEN_TITLE_CHARS:
                break
            out.append(w)
        text = " ".join(out)
        if text.count("*") % 2:
            text = text.replace("*", "")
    return text or None


def past_topics(conn: sqlite3.Connection, channel: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT topic FROM shorts WHERE channel=? AND topic IS NOT NULL ORDER BY id", (channel,))]


def other_topics(conn: sqlite3.Connection, channel: str) -> list[str]:
    """Recent topics of the other channels: the same story on two channels looks mass-produced."""
    return [r[0] for r in conn.execute(
        """SELECT topic FROM shorts WHERE channel != ? AND topic IS NOT NULL AND status != 'rejected'
            ORDER BY id DESC LIMIT 60""", (channel,))][::-1]


def format_use(conn: sqlite3.Connection, channel: str) -> dict[str, int]:
    """How often each format was used on this channel in the last 14 days."""
    return dict(conn.execute(
        """SELECT format_id, COUNT(*) FROM shorts WHERE channel=? AND status != 'rejected'
              AND created_at >= datetime('now', '-14 days') GROUP BY format_id""", (channel,)).fetchall())


def write_scripts(conn: sqlite3.Connection, settings: Settings, channel: Channel, fmt_id: str | None,
                  hero_scene_ids: list[int], batch_size: int = 5, on_batch=None) -> list[int]:
    """fmt_id: one format for all; None = Claude picks per Short from the channel's formats
    (its own plus shared ones, shorts/formats.py). on_batch(short_ids) is called after each batch
    is saved."""
    fmt = [channel.format(fmt_id)] if fmt_id else fmtlib.for_channel(channel)
    fmt_ids = [f["id"] for f in fmt]
    created = []
    for i in range(0, len(hero_scene_ids), batch_size):
        progress.report(i, len(hero_scene_ids), "script")
        heroes = [hero_context(conn, sid) for sid in hero_scene_ids[i:i + batch_size]]
        prompt = build_prompt(channel, fmt, heroes, past_topics(conn, channel.id),
                              inspiration.prompt_note(conn, channel.id)
                              + winners_note(conn, channel.id) + rejects_note(conn, channel.id)
                              + learn.retention_note(conn, channel.id) + learn.patterns_note(conn, channel.id),
                              other_topics(conn, channel.id), format_use(conn, channel.id),
                              format_results(conn, channel.id))
        try:
            replies = llm.ask_json(prompt, settings.claude_cmd, conn=conn, stage="claude script")
        except llm.LLMLimitError:
            break  # the heroes not written stay unused and are picked again next run
        batch_ids: list[int] = []
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
                if reply.get("skip"):
                    mark_off_niche(conn, channel, heroes[n - 1]["asset_id"], str(reply["skip"]))
                    done_heroes.add(n)
                    continue
                batch_ids.append(save_script(conn, channel, fmt_ids, heroes[n - 1], reply))
                done_heroes.add(n)
            except (ValueError, TypeError, AttributeError) as e:
                log.warning("skipping a script: %s", e)
        created += batch_ids
        if on_batch and batch_ids:
            on_batch(batch_ids)  # pipelined make: fact check this batch while the next is written
    return created


def mark_off_niche(conn: sqlite3.Connection, channel: Channel, asset_id: int, reason: str) -> None:
    from . import fit
    fit.mark(conn, channel, asset_id, False, reason, method="script")
    log.info("%s: video %s doesn't fit the niche (%s); not used on this channel", channel.id, asset_id, reason)


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
            b["seconds"] = block_seconds(new) + (END_LINGER if b is blocks[-1] else 0)
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
