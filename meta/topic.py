"""Quiz, ranking and text-card Shorts topic first (the new footage collector, shorts/collector.py):
Claude picks the subjects without seeing footage, the collector finds a checked clip of each, and
the Short is rendered with the same overlays and layout as the footage-first makers.

When a subject has no footage, the next spare subject is tried; when too few are left, Claude
writes a NEW script of the same type (never another type: Roeland, 5 November 2026). Each maker
returns [] when it gives up, and shorts/metatype.py then tries today's footage-first maker of the
same type.
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path

from .common import log, pick_track, render_segments

SCRIPTS = 3  # scripts written per Short before giving up on the topic-first route
# Niches whose subjects must come from the time of film, or no footage exists (round 3: retro tech
# rankings picked the Antikythera Mechanism and the Gutenberg press; 2 of 5 found).
ERA = {"retrotech": "Only pick machines and devices from the film era, about 1890 to 1990, so real "
                    "archive film or photos of them exist; nothing older."}


def era(niche: str) -> str:
    return f"\n{ERA[niche]}" if niche in ERA else ""

RANK_PROMPT = """You make a "ranking" Short about {theme}. Pick {n} different, well-known subjects that
can be ranked by ONE measurable property: {props}. Every value must measure the SAME thing in the
SAME unit, no two items share a value, and use only well-documented numbers you are sure of.
Pick subjects that are easy to film or photograph and that archive footage likely exists for
(famous named things, not abstract ones). #1 = the most/highest (or as stated above). The Short
shows the best 5 that we find footage for, so rank all {n}.{era}

Do not reuse these earlier Shorts: {avoid}{failed}

Return JSON only: {{"title": "YouTube title, max 60 characters",
"heading": "on-screen heading, max 5 words, true for EVERY item",
"items": [{{"rank": 1, "name": "max 4 words", "value": "number with unit",
"show": "what a good shot of it looks like, max 10 words"}}]}}"""

QUIZ_PROMPT = """You make a "{question}" quiz Short in 3 levels about {theme}. Pick {n} different,
well-known subjects, easy to hard, that a viewer can recognise from a picture and that archive
footage likely exists for. The Short uses the first 3 we find footage for, so give all {n}. For
each: "answer" (the name, max 4 words), "fact" (one surprising TRUE fact, max 14 plain words) and
"show" (what a good shot of it looks like, max 10 words).{era}

Do not reuse these earlier Shorts: {avoid}{failed}

Return JSON only: {{"title": "YouTube title, max 60 characters; the quiz always has exactly 3 levels, e.g. 'Only 1% can name all 3 planes'",
"rounds": [{{"answer": "...", "fact": "...", "show": "..."}}]}}"""

POST_PROMPT = """You write very short "did you know" posts for a YouTube Shorts channel about {topic}.
Each Short shows the post as a card on screen for about 10 seconds, with an archive clip of the
post's subject below it. These cards get millions of views when the fact makes people stop and rewatch.

What wins in this niche right now: {hot}.

Write {n} posts. Rules:
- One concrete, surprising, TRUE fact per post about ONE specific, filmable subject (a named
  plane, animal, place, machine, event). Only well-documented facts, no rumours, no exaggeration.
- 18 to 38 words. Plain words a 10-year-old understands. No hashtags, no emojis except at most one
  at the end.
- The first words must create curiosity; end on the most surprising part.
- Different subjects for every post. Avoid: {avoid}{failed}{era}

Return JSON only:
{{"posts": [{{"text": "...", "highlight": ["1-3 key words from the text"],
"subject": "the thing the clip must show, max 5 words", "show": "what a good shot of it looks like, max 10 words",
"title": "YouTube title, a question or claim, max 60 characters",
"tags": ["3 search tags about this post's subject"], "source": "where this fact is documented"}}]}}"""

FACTS = """You check short "did you know" posts before they are published on a {topic} channel.
For each: is every claim, name, date and number correct? If a small edit makes it correct, give
the corrected full text in "fix". If unsure, ok=false.

{posts}

Return JSON only: {{"checks": [{{"post": 1, "ok": true, "fix": ""}}]}}"""


def _failed_note(failed: list[str]) -> str:
    return f"\nNo footage was found for these, so don't use them: {'; '.join(failed[-20:])}" if failed else ""


def _avoid(lib: dict) -> str:
    return "; ".join(lib.get("avoid", [])[-40:]) or "none yet"


def _find(collector, name: str, show, need: float, hook: bool = False):
    """collector.find; hook = the Short's first shot (video strongly preferred over a photo)."""
    try:
        return collector.find(name, show, need=need, hook=hook)
    except TypeError:  # a collector without the hook option (tests' fakes)
        return collector.find(name, show, need=need)


def _clips(lib: dict, wanted: list[tuple[str, str | None]], need: float, enough: int, failed: list[str],
           least: int | None = None) -> list:
    """[(index into wanted, clip)] for the first `enough` subjects that have footage; stops early
    when fewer than `least` (default enough) can still be reached."""
    least = least or enough
    from shorts import collector as col
    got = []
    for i, (name, show) in enumerate(wanted):
        if len(got) >= enough:
            break
        if len(wanted) - i < least - len(got):
            break  # too few left to fill the Short: a new script instead
        sid = _find(lib["collector"], name, show, need, hook=not got)
        if sid is None:
            failed.append(name)
        else:
            got.append((i, col.clip(lib["conn"], sid)))
    return got


def _out(lib: dict, kind: str) -> Path:
    out_dir = Path(lib["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{kind}-{datetime.now():%Y%m%d-%H%M%S}-topic.mp4"


def ranking(niche: str, lib: dict) -> list[dict]:
    from . import ranking as rk
    settings, conn, llm = lib["settings"], lib["conn"], lib["llm"]
    theme = rk.THEMES[niche][0]
    need = rk.INTRO + rk.ITEM + rk.OUTRO + 0.5
    failed: list[str] = []
    for attempt in range(1, SCRIPTS + 1):
        data = llm.ask_json(RANK_PROMPT.format(theme=theme, props=rk.PROPERTIES[niche], n=7, avoid=_avoid(lib), era=era(niche),
                                               failed=_failed_note(failed)), settings.claude_cmd, retries=1)
        plan = sorted((it for it in data.get("items", []) if it.get("name")), key=lambda x: int(x.get("rank", 99)))
        log(f"[{niche}] ranking script {attempt}: {', '.join(it['name'] for it in plan)}")
        found = _clips(lib, [(it["name"], it.get("show")) for it in plan], need, 5, failed, least=4)
        if len(found) < 4:  # 4 is enough for a ranking
            log(f"[{niche}] footage for only {len(found)} subjects: writing a new ranking")
            continue
        items = [dict(plan[i], clip=c) for i, c in found]
        for r, it in enumerate(items, 1):  # the plan's order, renumbered over what was found
            it["rank"] = r
        seq = [it for it in items if it["rank"] != 1]
        random.shuffle(seq)
        seq.append(items[0])
        heading = data.get("heading") or data.get("title", "")
        shown: set[int] = set()
        segs = [{"clip": seq[0]["clip"], "offset": 0, "seconds": rk.INTRO,
                 "overlay": rk.overlay(heading, items, shown, None)}]
        for it in seq:
            shown.add(it["rank"])
            segs.append({"clip": it["clip"], "offset": rk.INTRO if it is seq[0] else 0, "seconds": rk.ITEM,
                         "overlay": rk.overlay(heading, items, set(shown), it["rank"])})
        segs[-1]["seconds"] += rk.OUTRO
        out = _out(lib, "ranking")
        track = pick_track(conn)
        secs = render_segments(segs, track, out)
        return [{"niche": niche, "file": str(out), "title": data.get("title", ""), "heading": heading,
                 "seconds": round(secs, 1),
                 "items": [{"rank": it["rank"], "name": it["name"], "value": it.get("value", ""),
                            "footage": it["clip"]["title"]} for it in items],
                 "scene_ids": [it["clip"]["id"] for it in items], "music": track["path"] if track else None,
                 "made_at": datetime.now().isoformat(timespec="seconds")}]
    return []


def quiz(niche: str, lib: dict) -> list[dict]:
    from . import quiz as qz
    settings, conn, llm = lib["settings"], lib["conn"], lib["llm"]
    theme, _, question = qz.THEMES[niche]
    need = qz.ASK + qz.COUNT + qz.REVEAL + 0.5
    failed: list[str] = []
    for attempt in range(1, SCRIPTS + 1):
        data = llm.ask_json(QUIZ_PROMPT.format(question=question, theme=theme, n=5, avoid=_avoid(lib), era=era(niche),
                                               failed=_failed_note(failed)), settings.claude_cmd, retries=1)
        plan = [r for r in data.get("rounds", []) if r.get("answer")]
        log(f"[{niche}] quiz script {attempt}: {', '.join(r['answer'] for r in plan)}")
        found = _clips(lib, [(r["answer"], r.get("show")) for r in plan], need, 3, failed)
        if len(found) < 3:
            log(f"[{niche}] footage for only {len(found)} subjects: writing a new quiz")
            continue
        rounds = [dict(plan[i], clip=c) for i, c in found]
        segs = []
        for lvl, r in enumerate(rounds, 1):
            t = 0.0
            segs.append({"clip": r["clip"], "offset": t, "seconds": qz.ASK,
                         "overlay": qz.overlay(question, lvl, None, None, None)})
            t += qz.ASK
            for c in range(qz.COUNT, 0, -1):
                segs.append({"clip": r["clip"], "offset": t, "seconds": 1.0,
                             "overlay": qz.overlay(question, lvl, str(c), None, None)})
                t += 1.0
            segs.append({"clip": r["clip"], "offset": t, "seconds": qz.REVEAL,
                         "overlay": qz.overlay(question, lvl, None, r["answer"], r.get("fact"))})
        out = _out(lib, "quiz")
        track = pick_track(conn)
        secs = render_segments(segs, track, out)
        return [{"niche": niche, "file": str(out), "title": data.get("title", ""), "seconds": round(secs, 1),
                 "rounds": [{"answer": r["answer"], "fact": r.get("fact"), "footage": r["clip"]["title"]}
                            for r in rounds],
                 "scene_ids": [r["clip"]["id"] for r in rounds], "music": track["path"] if track else None,
                 "made_at": datetime.now().isoformat(timespec="seconds")}]
    return []


def textcard(niche: str, lib: dict) -> list[dict]:
    from . import textcard as tc
    settings, conn, llm = lib["settings"], lib["conn"], lib["llm"]
    topic, hot = tc.NICHES[niche]
    failed: list[str] = []
    colour = random.Random(niche).choice([(232, 65, 24), (29, 155, 240), (0, 186, 124),
                                          (120, 86, 255), (249, 24, 128)])
    for attempt in range(1, SCRIPTS + 1):
        posts = llm.ask_json(POST_PROMPT.format(topic=topic, hot=hot, n=3, avoid=_avoid(lib), era=era(niche),
                                                failed=_failed_note(failed)),
                             settings.claude_cmd, retries=1).get("posts", [])
        posts = [p for p in posts if p.get("text") and p.get("subject")]
        for p in posts:
            p["text"] = re.sub(r"\s+", " ", p["text"]).strip()
        if not posts:
            continue
        checks = llm.ask_json(FACTS.format(topic=topic, posts="\n".join(f"Post {i}: {p['text']}"
                                                                       for i, p in enumerate(posts, 1))),
                              settings.claude_cmd, retries=1).get("checks", [])
        by_post = {int(c.get("post", 0)): c for c in checks if str(c.get("post", "")).isdigit()}
        log(f"[{niche}] text card script {attempt}: {', '.join(p['subject'] for p in posts)}")
        for i, post in enumerate(posts, 1):
            c = by_post.get(i, {})
            fix = str(c.get("fix") or "").strip()
            if not (c.get("ok") or fix):
                log(f"[{niche}] fact check failed: {post.get('title', '')}")
                continue
            text = fix or post["text"]
            secs = tc.seconds_for(text)
            sid = _find(lib["collector"], post["subject"], post.get("show"), secs, hook=True)  # the card's only clip
            if sid is None:
                failed.append(post["subject"])
                continue
            from shorts import collector as col
            clip = col.clip(conn, sid)
            out = _out(lib, "textcard")
            card = tc.card_image(text, post.get("highlight", []), lib.get("name") or f"{topic} Facts",
                                 lib.get("handle") or f"@{niche}facts", colour)
            track = pick_track(conn)
            tc.render(card, clip, track, secs, out)
            return [{"niche": niche, "file": str(out), "title": post.get("title", ""), "text": text,
                     "fixed": bool(fix), "visual": post["subject"], "scene_id": sid, "footage": clip["title"],
                     "footage_source": clip["source"], "seconds": round(secs, 1),
                     "fact_source": post.get("source", ""), "music": track["path"] if track else None,
                     "scene_ids": [sid], "tags": [str(t) for t in post.get("tags") or []][:3],
                     "made_at": datetime.now().isoformat(timespec="seconds")}]
    return []
