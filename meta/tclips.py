"""Transcript-first clip Shorts (plan/transcript-first-build-plan.md, phase 2; paper sections 7-9).

From a long free video with speech (a newsreel issue, a NASA briefing or documentary, a Prelinger
educational film) it reads what is said, finds the moments that work as a Short, and adds OUR layer
(hook, context, payoff) on top of the film's own sound. The moment alone is not the product:
YouTube doesn't count clips with captions of their own dialogue as original (paper section 5).

Two steps, each a runner job, one part per kind of source:

    python -m meta.tclips prep --part 1   # newsreel: pick 3 sources, transcribe (cached)
    python -m meta.tclips make --part 1   # newsreel: moments -> Shorts from those transcripts

Parts: 1 newsreel, 2 NASA, 3 Prelinger, 4 feature films (only titles in config/clip-titles.toml).

Per source, `make` does: transcript pass (Claude reads the whole transcript, 8-12 candidates with
our angle) -> signal pass (local: blank start, faces, loudness peaks, cuts) -> frame pass (Claude
ranks the best from frames) -> our layer (hook, context, payoff; at least 2 layer elements or no
Short) -> review against the frames -> render: hook above the film, dialogue subtitles from the
word times, context at the start and payoff at the end below the film, original sound, <= 58 s.

MP4s and made.json in ~/meta-out/tclips/. Only the `upload` step (Roeland, 2026-11-13) uploads to
YouTube and writes to the `shorts` database: the Shorts that `batch` put in upload-plan.json.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import quote

from PIL import Image, ImageDraw

from . import transcript as tx
from .clips import PD, UA, build_band, cuts, frame, grid, pick_file, probe_length, search, sfx, tag, text_block, year_of
from .textcard import H, W, black_bars, font, log, wrap

OUT = Path.home() / "meta-out" / "tclips"
TITLES = Path(__file__).resolve().parents[1] / "config" / "clip-titles.toml"
LOOK_TEST = Path(__file__).resolve().parents[1] / "config" / "look-test.toml"
SUB_EXT = (".srt", ".vtt")

MIN_LEN, MAX_LEN, CAP = 15.0, 45.0, 58.0   # paper 7.2/8: 15-45 s, never over 60 s (Content ID)
AIR = 0.3                                   # seconds of air before the first and after the last word
KEEP_SIGNAL = 5                             # candidates that go on to the frame pass
LAYER = ("context", "verdict", "payoff_fact", "story_frame", "comparison", "commentary")
BLOCK = ("killed", "kill", "dead", "death", "deaths", "died", "dying", "corpse", "corpses", "murder",
         "murdered", "suicide", "massacre", "executed", "execution", "lynched", "slaughter")

# How the dialogue subtitles look (Roeland's look test, 2026-11-13). "hot" colours the word being
# said; "upper" shouts in capitals; "box" puts the line on a dark plate.
LOOKS = {
    "classic": {"size": 46, "colour": (255, 255, 255), "stroke": 3},
    "yellow": {"size": 54, "colour": (255, 221, 0), "stroke": 4},
    "box": {"size": 46, "colour": (255, 255, 255), "stroke": 0, "box": (0, 0, 0, 205)},
    "karaoke": {"size": 50, "colour": (255, 255, 255), "stroke": 4, "hot": (255, 221, 0)},
    "pop": {"size": 64, "colour": (255, 255, 255), "stroke": 6, "hot": (80, 220, 120), "upper": True, "max_words": 3},
    "cream": {"size": 46, "colour": (245, 230, 200), "stroke": 3},
}
LOOK = "classic"                            # default until a look wins the test

# Whole looks for a Short (Roeland, 2026-11-13): hook above the film, dialogue subtitles, context and
# payoff below the film, credit and background, styled as one. "standard" is the look used so far.
def _t(size, colour, **kw):
    return {"size": size, "colour": colour, **kw}


CREAM, BROWN, RED, NAVY, GREEN, YELLOW = (245, 230, 200), (40, 28, 15), (200, 30, 30), (25, 50, 95), (80, 220, 120), (255, 221, 0)
THEMES = {
    "standard": {"bg": "black", "subs": "classic", "credit": (170, 170, 170),
                 "hook": _t(70, "white", stroke=3), "context": _t(46, "white", box=(0, 0, 0, 200)),
                 "payoff": _t(46, YELLOW, box=(0, 0, 0, 200))},
    "documentary": {"label": "Documentary", "bg": "black", "subs": "classic", "credit": (150, 150, 150),
                    "hook": _t(64, "white"), "context": _t(42, (215, 215, 215), kind="regular"),
                    "payoff": _t(44, "white", kind="regular")},
    "news": {"label": "Breaking news", "bg": "0x0b1a33", "subs": "box", "credit": (150, 170, 200),
             "hook": _t(56, "white", upper=True, box=RED + (255,)), "context": _t(42, "white", box=NAVY + (255,)),
             "payoff": _t(44, "white", box=RED + (255,))},
    "tabloid": {"label": "Tabloid", "bg": "black", "subs": "yellow", "credit": (170, 170, 170),
                "hook": _t(72, YELLOW, upper=True, stroke=5), "context": _t(46, "white", stroke=3),
                "payoff": _t(46, (0, 0, 0), box=YELLOW + (255,))},
    "neon": {"label": "Neon", "bg": "0x0d0d12", "subs": "pop", "credit": (120, 200, 150),
             "hook": _t(68, GREEN, upper=True, stroke=5), "context": _t(44, "white", box=(20, 60, 35, 230)),
             "payoff": _t(46, (0, 0, 0), box=GREEN + (255,))},
    "retro": {"label": "Retro film", "bg": "0x1e150c", "subs": "cream", "credit": (190, 165, 120),
              "hook": _t(64, CREAM), "context": _t(44, CREAM, kind="regular"),
              "payoff": _t(44, BROWN, box=CREAM + (255,))},
}
THEME = "tabloid"                           # Roeland picked it in the style test (2026-11-13)
TEST_THEMES = ("documentary", "news", "tabloid", "neon", "retro")


def styled_block(text: str, st: dict, width: int = W - 120) -> Image.Image:
    """A centred text block in a theme style: size, colour, kind (bold/regular), upper, stroke, box."""
    text = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F]", "", text or "").strip()
    if st.get("upper"):
        text = text.upper()
    f = font(st.get("kind", "bold"), st["size"])
    lines = wrap(text, f, width)
    lh = int(st["size"] * 1.28)
    im = Image.new("RGBA", (W, lh * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(lines):
        y, tw = 20 + i * lh, f.getlength(ln)
        if st.get("box"):
            d.rounded_rectangle(((W - tw) / 2 - 20, y - 6, (W + tw) / 2 + 20, y + lh - 2), 12, fill=st["box"])
        d.text(((W - tw) / 2, y), ln, font=f, fill=st["colour"], stroke_width=st.get("stroke", 0), stroke_fill="black")
    return im

KINDS = {
    "newsreel": {
        "part": 1, "source": "archive",
        "label": "Strange news from the past (newsreels)",
        "searches": [
            'title:("Universal Newsreel") AND (stunt OR invention OR odd OR novel OR contest OR fashion OR gadget) AND mediatype:movies',
            'title:("Universal Newsreel") AND mediatype:movies',
        ],
        "min": 240, "max": 2400, "max_year": 1967,
        "rights": "Universal Newsreel: MCA gave the newsreels to the US National Archives without copyright restrictions (1974).",
    },
    "nasa": {
        "part": 2, "source": "nasa",
        "label": "Strange news from the past (NASA and government film)",
        "queries": ["documentary", "history film", "press conference", "briefing", "interview"],
        "min": 180, "max": 3600, "max_year": 2100,
        "rights": "NASA video: US federal government work, no US copyright. No NASA logo, no endorsement implied.",
    },
    "prelinger": {
        "part": 3, "source": "archive",
        "label": "Strange news from the past (old educational films)",
        "searches": [
            f'collection:(prelinger) AND {PD} AND (subject:(education) OR subject:(educational) OR subject:(etiquette) OR subject:(social guidance))',
            f'collection:(prelinger) AND {PD} AND (subject:(industrial) OR subject:(sponsored))',
            f'collection:(prelinger) AND {PD}',
        ],
        "min": 300, "max": 2400, "max_year": 1977,
        "rights": "Prelinger Archives item marked public domain (pre-1978 film).",
    },
    "feature": {
        "part": 4, "source": "archive", "allowlist_only": True,
        "label": "Classic film dialogue",
        "searches": [],
        "min": 1800, "max": 9000, "max_year": 1963,
        "rights": "Public-domain sound feature, renewal checked by hand (config/clip-titles.toml).",
    },
}
PARTS = {spec["part"]: kind for kind, spec in KINDS.items()}

MOMENTS = """You find moments for YouTube Shorts in the transcript of a long free video.
Channel: {label}.
Source: "{title}" ({year}). Maker: {maker}. Description: {desc}

Transcript, one sentence per line: [sentence number] minutes:seconds text
{transcript}

Find {n} candidate moments that would each work as a 15-45 second Short that keeps the ORIGINAL
SOUND. Read whole stretches, don't score single lines: a moment is usually 3-12 sentences. Speech
should run almost without pause: the time stamps show long gaps between lines, and a moment with a
long silence or a stretch of only music is dull (silences get cut out, which can leave it too short). Score
each moment 1-5 on:
1. self_contained: understandable on its own, or with one line of context from us.
2. clear_start: starts at the start of a sentence, on a person speaking or on the action.
3. setup_payoff: a line, then a reaction, reveal or punchline inside 15-45 s; it ends hard after it.
4. surprising: surprising to a viewer TODAY: odd claims, outdated advice, a stunt, a confident
   prediction that turned out wrong, a "they really did this" moment.
5. not_famous: not a famous scene many channels already upload.
6. ad_safe: no deaths, disasters, injuries, war violence, slurs, or period racism or sexism played straight.

For every moment also draft OUR angle, what we add that the clip can't say itself:
"context" (who, when, why it matters; one line), "verdict" (what is funny, absurd or impressive
about it, and why), "payoff" (a TRUE fact beyond the clip: what happened next, what was later
proven wrong). If you have nothing true and interesting to add to a moment, leave it out.

"from" and "to" are sentence numbers (inclusive). Return JSON only:
{{"moments": [{{"from": 41, "to": 47, "why": "one line",
"scores": {{"self_contained": 4, "clear_start": 5, "setup_payoff": 4, "surprising": 5, "not_famous": 5, "ad_safe": 5}},
"angle": {{"context": "...", "verdict": "...", "payoff": "..."}}}}]}}
If nothing in this video works, return {{"moments": []}}."""

RANK = """You choose the best moments for YouTube Shorts from a long free video.
Channel: {label}. Source: "{title}" ({year}). Maker: {maker}.

Each image is ONE candidate moment (in the order below): 6 frames across it, labelled with seconds.
What is said in each, and our planned angle:
{lines}

For each candidate check:
- "ok": the picture works on a phone: a person or clear action most of the time, not a black
  screen, a title card or a static chart for the whole moment, and it fits what is said.
- "decade_seen": the decade the footage looks like it was filmed in (e.g. 1950).
- "era_ok": false when it clearly looks newer than {year}.
- "promo_risk": true when a current astronaut or NASA employee is shown in a way that could look
  like they endorse a channel (a posed promo shot rather than news or a briefing).
Then rank the best {keep} that are ok, best first.

Return JSON only: {{"ranked": ["M3", "M1"], "checks": [{{"id": "M1", "ok": true, "decade_seen": 1950,
"era_ok": true, "promo_risk": false, "why": "one line"}}]}}"""

LAYER_PROMPT = """You write OUR layer for a YouTube Short cut from a long free video.
Channel: {label}. Source: "{title}" ({year}). Maker: {maker}. Description: {desc}

The moment ({secs:.0f} s, the film's original sound is kept), what is said:
{lines}
Draft angle: {angle}
The image shows frames from the moment, labelled with seconds.

Tone: RAGEBAIT (Roeland's pick, 2026-11-13). A provocative, opinionated take that makes people
want to comment, argue or share: then-vs-now, "we stopped doing this", "this used to be normal",
or an open question at the end of the payoff. Every claim stays TRUE. No insults to groups of
people, no party politics, nothing about tragedies or violence: it must stay advertiser-friendly.

YouTube does NOT count captions of the dialogue, or text that only describes what is on screen, as
original. Our layer has to ADD something the viewer can't get from the clip. Write:
- "hook": on screen above the film the whole time. Max 9 words, curiosity first, no emoji.
- "context": shown during the first 5 seconds. Max 14 words: who, when, or why it matters. Not a
  description of what is on screen.
- "payoff": shown at the end. Max 22 words: a TRUE fact that goes beyond the clip (what happened
  next, what was later proven wrong), or a verdict with its reason. Only facts you are sure of;
  never invent names, dates or numbers.
  Where it fits, end the payoff so it leads back into the hook (the Short loops to its first frame).
- "title": YouTube title with story framing, 6-10 words, may end with 1-2 emoji.
- "credit": short source line, e.g. "Universal Newsreel, 1957 (National Archives)".
- "layer": which of these our texts honestly do: "context", "verdict", "payoff_fact",
  "story_frame" (a setup before and a reveal after, in our words), "comparison" (then vs now).
If you can't honestly do at least two, set "usable" false.

Return JSON only: {{"usable": true, "hook": "...", "context": "...", "payoff": "...", "title": "...",
"credit": "...", "layer": ["context", "payoff_fact"]}}"""

REVIEW_PROMPT = """You check a YouTube Short before it is rendered. Channel: {label}.
Source: "{title}" ({year}). Description: {desc}
What is said in the clip: {lines}

Our texts (the clip itself keeps its own sound and gets subtitles of what is said):
Hook: {hook}
Context (first 5 s): {context}
Payoff (end): {payoff}

The image shows frames from the start (S) and the end (E) of the clip.
1. Is the context true, and more than a description of the screen? If not, rewrite it (max 14 words).
2. Is the payoff TRUE and about this film, event or subject, not generic era trivia? If not,
   rewrite it from the description or the transcript (max 22 words). If you can't make it true, set "ok" false.
3. Does the hook promise something the clip delivers? If not, rewrite it (max 9 words).
Keep the texts' provocative ragebait tone when you rewrite, but never at the cost of the truth,
and never insulting a group of people.

Return JSON only: {{"ok": true, "hook": "...", "context": "...", "payoff": "...", "notes": "one line"}}"""


# ---------- rights and the allowlist ----------

def load_titles(path: Path = TITLES) -> tuple[list[dict], list[str]]:
    """(hand-checked titles, deny words) from config/clip-titles.toml."""
    if not path.exists():
        return [], []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    titles = [t for t in data.get("title", []) if (t.get("id") or t.get("search")) and t.get("kind") in KINDS]
    deny = [str(w).lower() for w in data.get("deny", {}).get("words", [])]
    return titles, deny


def denied(meta: dict, deny: list[str]) -> str | None:
    """The deny word an item's title, maker or description contains, or None."""
    text = " ".join(str(meta.get(k) or "") for k in ("title", "creator", "publisher", "sponsor", "producer",
                                                      "description", "center", "photographer",
                                                      "secondary_creator")).lower()
    return next((w for w in deny if w and w in text), None)


def rights_ok(kind: str, meta: dict, deny: list[str], allowed_ids: set[str], ident: str) -> tuple[bool, str]:
    """(ok, why) for one item. Never the uploader's label alone (paper 4.1)."""
    if ident in allowed_ids:
        return True, "on the hand-checked list"
    if KINDS[kind].get("allowlist_only"):
        return False, "not on the hand-checked list (config/clip-titles.toml)"
    word = denied(meta, deny)
    if word:
        return False, f"deny list: {word}"
    if kind == "newsreel":
        title = str(meta.get("title") or "").lower()
        colls = meta.get("collection") or []
        colls = [colls] if isinstance(colls, str) else colls
        if "universal newsreel" not in title and "universal_newsreels" not in colls:
            return False, "not a Universal Newsreel item"
        return True, KINDS[kind]["rights"]
    if kind == "prelinger":
        colls = meta.get("collection") or []
        colls = [colls] if isinstance(colls, str) else colls
        if "prelinger" not in colls or "publicdomain" not in str(meta.get("licenseurl") or ""):
            return False, "not a public-domain Prelinger item"
        return True, KINDS[kind]["rights"]
    if kind == "nasa":
        from shorts.sources.nasa import needs_rights_check
        if needs_rights_check(meta):
            return False, "credit or description names a third party (not plainly NASA's own work)"
        return True, KINDS[kind]["rights"]
    return False, "unknown kind"


# ---------- finding sources ----------

def archive_item(ident: str) -> dict | None:
    """Video URL, length, metadata and a human subtitle file (not archive.org's own 'asr') of an item."""
    import requests
    r = requests.get(f"https://archive.org/metadata/{quote(ident)}", headers=UA, timeout=60)
    r.raise_for_status()
    data = r.json()
    files = data.get("files", [])
    got = pick_file(files)
    if not got:
        return None
    name, length = got
    url = f"https://archive.org/download/{quote(ident)}/{quote(name)}"
    subs = sorted((f["name"] for f in files if str(f.get("name", "")).lower().endswith(SUB_EXT)
                   and "asr" not in str(f.get("name", "")).lower()),
                  key=lambda n: (not n.lower().endswith(".srt"), len(n)))
    return {"url": url, "length": length or probe_length(url), "meta": data.get("metadata", {}),
            "captions_url": f"https://archive.org/download/{quote(ident)}/{quote(subs[0])}" if subs else None}


def find_archive(kind: str, n: int, titles: list[dict], deny: list[str]) -> list[dict]:
    spec = KINDS[kind]
    pinned = [t for t in titles if t["kind"] == kind and not str(t.get("id", "")).startswith("nasa:")]
    allowed = {t["id"] for t in pinned if t.get("id")}
    out, seen, done = [], set(), set()
    queue = [("pinned", {"identifier": t["id"]}) for t in pinned if t.get("id")]
    for i, t in enumerate(pinned):
        # a checked FILM given by search: the server picks its best copy (deny list still applies,
        # so restored or colorized copies are skipped), at most one source per entry
        if t.get("id") or not t.get("search"):
            continue
        try:
            docs = search(t["search"], rows=15)
        except Exception as e:  # noqa: BLE001
            log(f"[{kind}] search for '{t.get('title', t['search'])}' failed: {e}")
            continue
        for d in docs:
            y = year_of(d)
            if t.get("year") and y and abs(y - int(t["year"])) > 1:
                continue
            queue.append((f"title{i}", d))
    for q in spec["searches"]:
        try:
            queue += [(q, d) for d in search(q, rows=30)]
        except Exception as e:  # noqa: BLE001 - one bad search shouldn't end the kind
            log(f"[{kind}] search failed: {e}")
    for q, doc in queue:
        ident = doc["identifier"]
        if len(out) >= n:
            break
        if ident in seen or q in done:
            continue
        seen.add(ident)
        checked_film = q.startswith("title")
        year = year_of(doc)
        if year and year > spec["max_year"] and ident not in allowed:
            continue
        try:
            item = archive_item(ident)
        except Exception as e:  # noqa: BLE001
            log(f"[{kind}] {ident}: metadata failed ({e})")
            continue
        if not item or not item["length"]:
            continue
        if not spec["min"] <= item["length"] <= spec["max"] and ident not in allowed:
            continue
        meta = item["meta"]
        if checked_film:
            word = denied(meta, deny)
            ok, why = (False, f"deny list: {word}") if word else (True, "film on the hand-checked list")
            entry = pinned[int(q[5:])]
            if ok and entry.get("rights"):
                why = f"{why}: {entry['rights']}"
        else:
            ok, why = rights_ok(kind, meta, deny, allowed, ident)
        if not ok:
            log(f"[{kind}] {ident}: skipped, {why}")
            continue
        if checked_film:
            done.add(q)
        out.append({"id": ident, "kind": kind, "url": item["url"], "length": round(item["length"]),
                    "title": str(meta.get("title") or ident), "year": year_of(meta) or year,
                    "maker": str(meta.get("creator") or meta.get("publisher") or "unknown")[:120],
                    "desc": re.sub(r"<[^>]+>|\s+", " ", str(meta.get("description") or ""))[:900],
                    "link": f"https://archive.org/details/{ident}", "rights": why,
                    "captions_url": item["captions_url"], "captions_origin": "archive_subs"})
        log(f"[{kind}] picked {ident}: '{out[-1]['title']}' ({out[-1]['year']}), {item['length'] / 60:.1f} min, "
            f"{'subtitles' if item['captions_url'] else 'no human subtitles'}")
    return out


def find_nasa(n: int, titles: list[dict], deny: list[str]) -> list[dict]:
    """NASA videos with NASA's own captions first (50 of 50 had them in the paper's count)."""
    import requests
    from shorts.sources.nasa import asset_files, credit_of
    spec = KINDS["nasa"]
    allowed = {t["id"][5:] for t in titles if t["kind"] == "nasa" and str(t["id"]).startswith("nasa:")}
    out, seen = [], set()
    for q in spec["queries"]:
        try:
            r = requests.get("https://images-api.nasa.gov/search", params={"q": q, "media_type": "video"},
                             headers=UA, timeout=60)
            items = r.json().get("collection", {}).get("items", [])[:25]
        except Exception as e:  # noqa: BLE001
            log(f"[nasa] search {q!r} failed: {e}")
            continue
        for it in items:
            if len(out) >= n:
                return out
            data = (it.get("data") or [{}])[0]
            nid = data.get("nasa_id")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            ok, why = rights_ok("nasa", data, deny, allowed, nid)
            if not ok:
                log(f"[nasa] {nid}: skipped, {why}")
                continue
            caps = tx.nasa_captions_url(nid)
            if not caps and nid not in allowed:
                continue  # plenty have captions: keep the ones that do
            try:
                files = asset_files(nid)
            except Exception:  # noqa: BLE001
                continue
            vid = next((f for suf in ("~large.mp4", "~medium.mp4") for f in files if f.lower().endswith(suf)), None)
            if not vid:
                continue
            vid = vid.replace("http://", "https://").replace(" ", "%20")
            length = probe_length(vid)
            if not length or not spec["min"] <= length <= spec["max"]:
                continue
            out.append({"id": f"nasa:{nid}", "kind": "nasa", "url": vid, "length": round(length),
                        "title": data.get("title") or nid, "year": year_of({"date": data.get("date_created")}),
                        "maker": credit_of(data) or "NASA",
                        "desc": re.sub(r"<[^>]+>|\s+", " ", str(data.get("description") or ""))[:900],
                        "link": f"https://images.nasa.gov/details/{quote(nid)}", "rights": why,
                        "captions_url": caps, "captions_origin": "nasa_captions"})
            log(f"[nasa] picked {nid}: '{out[-1]['title']}', {length / 60:.1f} min")
    return out


def find_sources(kind: str, n: int) -> list[dict]:
    titles, deny = load_titles()
    if KINDS[kind]["source"] == "nasa":
        return find_nasa(n, titles, deny)
    return find_archive(kind, n, titles, deny)


# ---------- moments ----------

def snap(sents: list[dict], a: int, b: int, min_len: float = MIN_LEN, max_len: float = MAX_LEN,
         cap: float = CAP, air: float = AIR) -> dict | None:
    """Times for sentences a..b on word borders with a little air. Too short: add sentences after
    (then before) while they fit; too long: drop sentences from the end. None when it can't fit."""
    if not sents:
        return None
    n = len(sents)
    a, b = max(0, min(a, b)), min(n - 1, max(a, b))

    def span(x, y):
        return (sents[y]["end"] + air) - max(0.0, sents[x]["start"] - air)
    while b > a and span(a, b) > max_len:
        b -= 1
    if span(a, b) > cap:
        return None
    while span(a, b) < min_len:
        if b + 1 < n and span(a, b + 1) <= max_len:
            b += 1
        elif a > 0 and span(a - 1, b) <= max_len:
            a -= 1
        else:
            return None
    return {"from": a, "to": b, "start": round(max(0.0, sents[a]["start"] - air), 2),
            "end": round(sents[b]["end"] + air, 2)}


def blocked(text: str) -> str | None:
    words = set(re.findall(r"[a-z']+", text.lower()))
    return next((w for w in BLOCK if w in words), None)


def claude_score(m: dict) -> float:
    s = m.get("scores") or {}
    vals = [float(v) for v in s.values() if isinstance(v, (int, float))]
    return round(mean(vals), 2) if vals else 0.0


def candidates(moments: list[dict], sents: list[dict]) -> list[dict]:
    """Claude's moments -> snapped, filtered candidates with their text."""
    out, taken = [], []
    for m in moments:
        try:
            a, b = int(m["from"]), int(m["to"])
        except (KeyError, TypeError, ValueError):
            continue
        t = snap(sents, a, b)
        if not t:
            continue
        if any(not (t["end"] <= s or t["start"] >= e) for s, e in taken):
            continue  # overlaps one we already have
        text = " ".join(s["text"] for s in sents[t["from"]:t["to"] + 1])
        scores = m.get("scores") or {}
        if float(scores.get("ad_safe", 5) or 5) < 4:
            continue
        word = blocked(text)
        if word:
            log(f"moment {t['from']}-{t['to']} dropped: '{word}' in the text (not ad-safe)")
            continue
        taken.append((t["start"], t["end"]))
        out.append({**t, "text": text, "why": m.get("why", ""), "scores": scores,
                    "angle": m.get("angle") or {}, "claude": claude_score(m)})
    return out


def loud_peak_db(samples, rate: int, win: float = 0.5) -> float:
    """How far the loudest half second sits above the median one, in dB (laughter, applause, a bang)."""
    import numpy as np
    n = int(rate * win)
    if len(samples) < n * 2:
        return 0.0
    chunks = samples[: len(samples) // n * n].astype(np.float64).reshape(-1, n)
    rms = np.sqrt((chunks ** 2).mean(axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    return round(float(db.max() - np.median(db)), 1)


def blank(im: Image.Image | None) -> bool:
    """True for a black, white or flat frame (title card backgrounds, fades)."""
    import numpy as np
    if im is None:
        return True
    g = np.asarray(im.convert("L"), dtype=np.float32)
    return bool(g.mean() < 22 or g.std() < 10)


def signal_score(c: dict) -> float | None:
    """Claude's score adjusted by the local signals; None drops the candidate."""
    sig = c.get("signals") or {}
    if sig.get("blank_start"):
        return None
    score = c["claude"]
    if sig.get("face_start"):
        score += 0.3          # a person and speech early (paper 7.2)
    if sig.get("peak_db", 0) >= 9:
        score += 0.2          # laughter, applause, a bang
    if sig.get("cuts_per_10s", 0) > 4:
        score -= 0.3          # a montage: hard to follow on a phone
    return round(score, 2)


def signals(piece: str, offset: float, length: float) -> dict:
    """Local checks on a candidate's piece (the moment starts `offset` seconds into it)."""
    import numpy as np
    out: dict = {}
    first = frame(piece, offset + 0.4, 480)
    out["blank_start"] = bool(blank(first))  # numpy bool otherwise: made.json can't hold it
    try:
        from shorts.framing import _faces
        out["face_start"] = bool(first is not None and _faces(np.asarray(first)[:, :, ::-1].copy()))
    except Exception:  # noqa: BLE001 - no face detector: no bonus
        out["face_start"] = False
    p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", piece, "-vn", "-ac", "1", "-ar", "8000",
                        "-f", "s16le", "-"], capture_output=True, timeout=300)
    out["peak_db"] = loud_peak_db(np.frombuffer(p.stdout, np.int16), 8000) if p.stdout else 0.0
    out["cuts_per_10s"] = round(len(cuts(piece)) * 10 / max(length, 1), 1)
    return out


def sheet(piece: str, length: float, n: int = 6) -> bytes:
    times = [0.5 + (length - 1.0) * i / (n - 1) for i in range(n)]
    return grid([(f"{t:.0f}s", frame(piece, t, 320)) for t in times], cols=n)


# ---------- our layer ----------

def layer_ok(plan: dict) -> list[str]:
    """The layer elements a plan really has (claimed and backed by text). The Short is made only
    with at least two (paper 5.4)."""
    claimed = [x for x in plan.get("layer") or [] if x in LAYER]
    have = []
    for x in dict.fromkeys(claimed):
        if x == "context" and not (plan.get("context") or "").strip():
            continue
        if x in ("payoff_fact", "verdict") and not (plan.get("payoff") or "").strip():
            continue
        if x == "story_frame" and not ((plan.get("hook") or "").strip() and (plan.get("payoff") or "").strip()):
            continue
        have.append(x)
    return have


# ---------- render ----------

def subtitle_blocks(words: list[list], start: float, end: float, max_words: int = 6,
                    with_words: bool = False) -> list[tuple]:
    """Dialogue subtitles in output time (0 = start): short blocks of at most `max_words`, broken
    at sentence ends and pauses; each stays until the next one starts (or a moment after its last word)."""
    ws = [w for w in words if w[1] >= start - 0.05 and w[2] <= end + 0.05]
    blocks: list[list] = []
    cur: list = []
    for w in ws:
        if cur and (len(cur) >= max_words or w[1] - cur[-1][2] > 0.7):
            blocks.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…]$", str(w[0])) or (len(cur) >= 3 and re.search(r"[,;:]$", str(w[0]))):
            blocks.append(cur)  # sentence end, or a comma once the block has a few words
            cur = []
    if cur:
        blocks.append(cur)
    out = []
    for i, b in enumerate(blocks):
        t0 = max(0.0, b[0][1] - start)
        nxt = blocks[i + 1][0][1] - start if i + 1 < len(blocks) else None
        t1 = min(nxt, b[-1][2] - start + 0.6) if nxt is not None else min(end - start, b[-1][2] - start + 0.6)
        if t1 - t0 >= 0.2:
            out.append((round(t0, 2), round(t1, 2), " ".join(str(w[0]) for w in b)) + ((b,) if with_words else ()))
    return out


# No dull stretches (Roeland, 2026-11-13: the X-15 Short had a long silence). Every pause between
# words longer than MAX_GAP is cut down to KEEP_GAP; a Short that ends up shorter than MIN_TIGHT is dropped.
MAX_GAP = 0.8
KEEP_GAP = 0.3
MIN_TIGHT = 10.0


def tighten(words: list[list], start: float, end: float, max_gap: float = MAX_GAP,
            keep: float = KEEP_GAP) -> tuple[list[tuple[float, float]], list[list]]:
    """The stretches of start..end to play, with long silences cut out, and the words moved to
    output time (0 = start of the Short)."""
    ws = sorted((w for w in words if w[1] >= start - 0.05 and w[2] <= end + 0.05), key=lambda w: w[1])
    if not ws:
        return [(start, end)], []
    segs, a, last = [], max(start, ws[0][1] - AIR), ws[0][2]
    for w in ws[1:]:
        if w[1] - last > max_gap:
            segs.append((a, last + keep / 2))
            a = w[1] - keep / 2
        last = max(last, w[2])
    segs.append((a, max(a + 0.1, min(end, last + AIR))))
    acc, starts = 0.0, []
    for x, y in segs:
        starts.append(acc)
        acc += y - x
    out = []
    for w in ws:
        i = next((k for k, (x, y) in enumerate(segs) if x - 1e-6 <= w[1] <= y + 1e-6), None)
        if i is None:
            continue
        x, y = segs[i]
        out.append([w[0], round(starts[i] + w[1] - x, 3), round(starts[i] + min(w[2], y) - x, 3)])
    return [(round(x, 3), round(y, 3)) for x, y in segs], out


# Engagement elements (research/engagement-elementen.md; Roeland 2026-11-13: everything rated
# "middel" or higher). FX on: punch-in zoom on the hook, cleaned and louder speech, a freeze frame
# with "WAIT..." on the payoff, one red circle on the detail that matters, a loop ending that fades
# back to the first frame, and quiet synthetic sound effects (made by ffmpeg: no licence question).
FX = False                                 # Roeland 2026-11-13 after the side-by-side: he does not like the effects
FX_FREEZE = 1.2                             # seconds the payoff frame is held
HOOK_ZOOM = (3.0, 0.12)                     # punch-in over the first 3 s, to 1.12x
LOOP_FADE = 0.5                             # last seconds fade back to the first frame
SFX_VOL = 0.25                              # about -12 dB under the dialogue
CLEAN_AUDIO = "highpass=f=80,afftdn=nf=-25,equalizer=f=3000:t=q:w=1.5:g=3,loudnorm=I=-14:TP=-1.5"
PLAIN_AUDIO = "loudnorm=I=-16:TP=-1.5"

FOCUS_PROMPT = """Frames from a YouTube Short cut from an old film, numbered. Our hook: "{hook}".
Our payoff: "{payoff}". What is said: {lines}

Is there ONE detail a viewer should look at (a face, an object, a sign, a machine) that the hook,
payoff or speech is about? Pick the frame where it is clearest and give its centre as fractions of
the frame width and height (0-1). Only if it really helps: when the whole frame is the point, say none.
Return JSON only: {{"frame": 3, "x": 0.42, "y": 0.55, "why": "one line"}} or {{"none": true}}"""


def tick(out: Path) -> Path:
    from shorts import media
    media.run(["-f", "lavfi", "-i", "aevalsrc='0.8*sin(2*PI*1800*t)*exp(-60*t)|0.8*sin(2*PI*1800*t)*exp(-60*t)':d=0.12:s=48000",
               "-ar", "48000", "-ac", "2", str(out)])
    return out


def ring(cx: float, cy: float, h: int, r: int = 95) -> Image.Image:
    """A red circle (Tabloid style) on a transparent canvas the size of the film band."""
    im = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x, y = cx * W, cy * h
    d.ellipse((x - r - 4, y - r - 4, x + r + 4, y + r + 4), outline=(0, 0, 0, 160), width=16)
    d.ellipse((x - r, y - r, x + r, y + r), outline=(230, 30, 30, 255), width=10)
    return im


def freeze_point(words_out: list[list], total: float) -> float:
    """Output time for the payoff freeze: the end of the word closest to where the payoff starts,
    so no word is cut in half."""
    want = max(total - 5.0, total * 0.65)
    ends = [w[2] for w in words_out if w[2] < total - 0.5] or [want]
    return min(ends, key=lambda e: abs(e - want))


def piece_time(segs: list[tuple[float, float]], t_out: float) -> tuple[int, float]:
    """Segment index and piece time of an output time (before any freeze)."""
    acc = 0.0
    for i, (x, y) in enumerate(segs):
        if t_out <= acc + (y - x) + 1e-6:
            return i, x + (t_out - acc)
        acc += y - x
    return len(segs) - 1, segs[-1][1]


def out_of_piece(segs: list[tuple[float, float]], t: float) -> float | None:
    acc = 0.0
    for x, y in segs:
        if x - 1e-6 <= t <= y + 1e-6:
            return acc + t - x
        acc += y - x
    return None


def ask_focus(llm, settings, piece: str, plan: dict, words: list[list], lines: str) -> dict | None:
    """Claude picks the one detail for the red circle, from frames after the hook zoom."""
    segs, _ = tighten(words, plan["start"], plan["end"])
    total = sum(y - x for x, y in segs)
    if total < HOOK_ZOOM[0] + 2:
        return None
    outs = [HOOK_ZOOM[0] + 0.5 + k * (total - HOOK_ZOOM[0] - 2.5) / 5 for k in range(6)]
    times = [piece_time(segs, t)[1] for t in outs]
    try:
        sheet = grid([(str(k + 1), frame(piece, t, 360)) for k, t in enumerate(times)], cols=3)
    except RuntimeError:
        return None
    got = ask(llm, settings, FOCUS_PROMPT.format(hook=plan.get("hook"), payoff=plan.get("payoff"), lines=lines[:1200]),
              [sheet])
    try:
        k = int(got["frame"]) - 1
        x, y = float(got["x"]), float(got["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if got.get("none") or not (0 <= k < 6 and 0.05 <= x <= 0.95 and 0.05 <= y <= 0.95):
        return None
    return {"t": round(times[k], 2), "x": x, "y": y, "why": got.get("why", "")}


def subtitle_image(words: list[str], look: dict, hot: int | None = None) -> Image.Image:
    """One subtitle on a full-width transparent canvas of fixed height (two lines), text at the
    bottom. hot = index of the word being said, drawn in the look's "hot" colour."""
    size = look["size"]
    f = font("bold", size)
    words = [w.upper() for w in words] if look.get("upper") else list(words)
    lines = wrap(" ".join(words), f, W - 160)[:2]
    lh = int(size * 1.28)
    im = Image.new("RGBA", (W, 2 * lh + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    k = 0
    for i, ln in enumerate(lines):
        y = im.height - (len(lines) - i) * lh - 10
        tw = f.getlength(ln)
        if look.get("box"):
            d.rounded_rectangle(((W - tw) / 2 - 18, y - 6, (W + tw) / 2 + 18, y + lh - 2), 14, fill=look["box"])
        x = (W - tw) / 2
        for word in ln.split():
            colour = look["hot"] if hot == k and look.get("hot") else look["colour"]
            d.text((x, y), word, font=f, fill=colour, stroke_width=look.get("stroke", 0), stroke_fill="black")
            x += f.getlength(word + " ")
            k += 1
    return im


def subtitle_track(words: list[list], start: float, end: float, look: dict, tmp: Path) -> tuple[Path, int]:
    """The dialogue subtitles as one image sequence (an ffmpeg concat list), so a look that colours
    every spoken word is one overlay, not hundreds. Returns (concat list, image height)."""
    total = end - start
    blank = subtitle_image([], look)
    blank.save(tmp / "sub-blank.png")
    shots: list[tuple[float, float, Image.Image]] = []
    for t0, t1, _text, ws in subtitle_blocks(words, start, end, look.get("max_words", 6), with_words=True):
        texts = [str(w[0]) for w in ws]
        if not look.get("hot"):
            shots.append((t0, t1, subtitle_image(texts, look)))
            continue
        for j, w in enumerate(ws):
            a = t0 if j == 0 else max(t0, w[1] - start)
            b = t1 if j == len(ws) - 1 else min(t1, ws[j + 1][1] - start)
            if b - a > 0.02:
                shots.append((a, b, subtitle_image(texts, look, hot=j)))
    lines, now = [], 0.0
    for n, (a, b, im) in enumerate(shots):
        a, b = max(a, now), min(b, total)
        if b - a < 0.02:
            continue
        if a - now > 0.01:
            lines += [f"file '{tmp / 'sub-blank.png'}'", f"duration {a - now:.3f}"]
        png = tmp / f"sub-{n}.png"
        im.save(png)
        lines += [f"file '{png}'", f"duration {b - a:.3f}"]
        now = b
    if total - now > 0.01 or not lines:
        lines += [f"file '{tmp / 'sub-blank.png'}'", f"duration {max(total - now, 0.05):.3f}"]
    lines.append(f"file '{tmp / 'sub-blank.png'}'")  # concat needs the last file twice
    lst = tmp / "subs.txt"
    lst.write_text("\n".join(lines) + "\n")
    return lst, blank.height


def render(piece: str, plan: dict, words: list[list], out: Path, bare: bool = False, look: str | None = None,
           theme: str = THEME, fx: bool | None = None, focus: dict | None = None) -> float:
    """One Short from a local piece of the source (times in piece time). bare = the same cut with
    subtitles only, for the side-by-side test of what our layer adds."""
    from shorts import media
    fx = (FX if fx is None else fx) and not bare
    segs, words = tighten(words, plan["start"], plan["end"])
    start = segs[0][0]
    total = round(sum(y - x for x, y in segs), 3)
    pieces = [{"kind": "play", "t0": x, "t1": y, "speed": 1.0} for x, y in segs]
    frz = None
    ring_at = None
    if fx and total >= 8:
        frz = round(freeze_point(words, total), 3)
        i, tp = piece_time(segs, frz)
        x, y = segs[i]
        pieces = ([{"kind": "play", "t0": a, "t1": b, "speed": 1.0} for a, b in segs[:i]]
                  + ([{"kind": "play", "t0": x, "t1": tp, "speed": 1.0}] if tp - x > 0.05 else [])
                  + [{"kind": "freeze", "t0": tp, "t1": tp, "speed": 1.0, "secs": FX_FREEZE}]
                  + ([{"kind": "play", "t0": tp, "t1": y, "speed": 1.0}] if y - tp > 0.05 else [])
                  + [{"kind": "play", "t0": a, "t1": b, "speed": 1.0} for a, b in segs[i + 1:]])
        words = [[w[0], w[1] + (FX_FREEZE if w[1] >= frz - 1e-3 else 0), w[2] + (FX_FREEZE if w[1] >= frz - 1e-3 else 0)]
                 for w in words]
        if focus:
            fo = out_of_piece(segs, focus["t"])
            if fo is not None and fo >= HOOK_ZOOM[0]:
                ring_at = fo + (FX_FREEZE if fo >= frz else 0)
        total = round(total + FX_FREEZE, 3)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        crop = black_bars(piece, start)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", piece], capture_output=True, text=True)
        cw, ch = map(int, probe.stdout.strip().split(",")[:2])
        if crop:
            cw, ch = map(int, crop.split("=")[1].split(":")[:2])
        band_h = min(int(W * ch / cw), 900) // 2 * 2
        band_y = 640
        band = build_band(piece, pieces, crop, band_h, True, tmp)
        th = THEMES[theme]
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if not bare and plan.get("hook"):
            hook = styled_block(plan["hook"], th["hook"])
            base.alpha_composite(hook, (0, max(150, band_y - hook.height - 50)))
        credit = text_block(plan.get("credit", ""), 30, th["credit"])
        base.alpha_composite(credit, (0, H - 200))
        base.save(tmp / "base.png")
        inputs = ["-f", "lavfi", "-t", f"{total:.2f}", "-i", f"color={th['bg']}:s={W}x{H}:r=30",
                  "-i", str(band), "-loop", "1", "-t", f"{total:.2f}", "-i", str(tmp / "base.png")]
        subs, sub_h = subtitle_track(words, 0.0, total, LOOKS[look or th['subs']], tmp)
        inputs += ["-f", "concat", "-safe", "0", "-i", str(subs)]
        bandv = "[1:v]"
        if fx:  # punch-in on the hook: zoom 1 -> 1.12 over the first seconds, then a cut back to 1x
            dur, amt = HOOK_ZOOM
            fc_band = (f"[1:v]scale={W * 2}:{band_h * 2},zoompan=z='if(lt(on/30,{dur}),1+{amt}*on/30/{dur},1)':d=1"
                       f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{band_h}:fps=30[bz];")
            bandv = "[bz]"
        else:
            fc_band = ""
        fc = (fc_band + f"[0:v]{bandv}overlay=0:{band_y}[a0];[a0][2:v]overlay=0:0[b0];[3:v]format=rgba,fps=30[subs];"
              f"[b0][subs]overlay=0:{band_y + band_h - sub_h - 16}:eof_action=repeat[c0]")
        last, idx = "c0", 4
        overlays = []  # (png, y, t0, t1)
        if fx:
            first = tmp / "first.png"
            media.run(["-i", str(band), "-frames:v", "1", str(first)])
            inputs += ["-loop", "1", "-t", f"{total:.2f}", "-i", str(first)]
            fc += (f";[{idx}:v]format=rgba,fade=t=in:st={total - LOOP_FADE:.2f}:d={LOOP_FADE}:alpha=1[lp{idx}]"
                   f";[{last}][lp{idx}]overlay=0:{band_y}[o{idx}]")
            last, idx = f"o{idx}", idx + 1
            if frz is not None:
                wait = tag("WAIT...")
                canvas = Image.new("RGBA", (W, wait.height + 10), (0, 0, 0, 0))
                canvas.alpha_composite(wait, ((W - wait.width) // 2, 0))
                canvas.save(tmp / "wait.png")
                overlays.append((tmp / "wait.png", band_y + 24, frz, frz + FX_FREEZE))
            if ring_at is not None:
                ring(focus["x"], focus["y"], band_h).save(tmp / "ring.png")
                overlays.append((tmp / "ring.png", band_y, ring_at, ring_at + 2.0))
        if not bare:
            ctx_end = min(5.0, total * 0.3)
            pay_at = frz if frz is not None else max(total - 5.0, total * 0.65)
            for name, text, t0, t1 in (("context", plan.get("context"), 0.0, ctx_end),
                                       ("payoff", plan.get("payoff"), pay_at, total + 1)):
                if text:
                    png = tmp / f"{name}.png"
                    styled_block(text, th[name]).save(png)
                    overlays.append((png, band_y + band_h + 50, t0, t1))
        for png, y, t0, t1 in overlays:
            inputs += ["-loop", "1", "-t", f"{total:.2f}", "-i", str(png)]
            fc += f";[{last}][{idx}:v]overlay=0:{y}:enable='between(t,{t0:.2f},{t1:.2f})'[o{idx}]"
            last, idx = f"o{idx}", idx + 1
        fc += f";[{last}]format=yuv420p[out];[1:a]{CLEAN_AUDIO if fx else PLAIN_AUDIO},aresample=48000[a1]"
        cues = []  # (sound file, at)
        if fx:
            cues.append((sfx("whoosh", tmp / "whoosh.wav"), 0.0))
            if frz is not None:
                cues += [(sfx("whoosh", tmp / "whoosh2.wav"), max(0.0, frz - 0.35)), (sfx("hit", tmp / "hit.wav"), frz)]
            if ring_at is not None:
                cues.append((tick(tmp / "tick.wav"), ring_at))
        mix = "[a1]"
        for f, at in cues:
            inputs += ["-i", str(f)]
            fc += f";[{idx}:a]volume={SFX_VOL},adelay={int(at * 1000)}|{int(at * 1000)}[s{idx}]"
            mix += f"[s{idx}]"
            idx += 1
        fc += (f";{mix}amix=inputs={len(cues) + 1}:duration=first:normalize=0[aud]" if cues else ";[a1]anull[aud]")
        media.run(inputs + ["-filter_complex", fc, "-map", "[out]", "-map", "[aud]", "-t", f"{total:.2f}",
                            "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac",
                            "-b:a", "160k", "-movflags", "+faststart", str(out)])
    return total


# ---------- one source ----------

def ask(llm, settings, prompt: str, images: list[bytes] | None = None) -> dict:
    return llm.ask_json(prompt, settings.claude_cmd, retries=1, images=images or None) or {}


def make_source(src: dict, kind: str, si: int, settings, llm, per_source: int, bare_first: bool) -> list[dict]:
    spec = KINDS[kind]
    t = tx.get(src)
    sents = t["sentences"]
    if len(sents) < 10:
        log(f"[{kind}] {src['id']}: only {len(sents)} sentences, skipped")
        return []
    info = dict(label=spec["label"], title=src["title"], year=src.get("year") or "year unknown",
                maker=src.get("maker", "unknown"), desc=src.get("desc", ""))
    text = tx.numbered(sents)[:90_000]  # ~25k tokens: a 60-minute briefing still fits
    found = ask(llm, settings, MOMENTS.format(**info, transcript=text, n="8 to 12")).get("moments") or []
    cands = candidates(found, sents)
    log(f"[{kind}] {src['id']}: Claude found {len(found)} moments, {len(cands)} fit 15-45 s and are ad-safe")
    if not cands:
        return []
    from shorts import media
    work = OUT / "work"
    work.mkdir(parents=True, exist_ok=True)
    cands = sorted(cands, key=lambda c: -c["claude"])[:10]
    for k, c in enumerate(cands):
        # a local piece with 1 s on each side, so the render never seeks over HTTP
        c["p0"] = max(0.0, c["start"] - 1.0)
        c["piece"] = str(work / f"{kind}-{si}-{k}.mp4")
        try:
            media.run(["-ss", f"{c['p0']:.2f}", "-t", f"{c['end'] - c['p0'] + 1.0:.2f}", "-i", src["url"],
                       "-vf", "scale=-2:min(720\\,ih)", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                       "-c:a", "aac", "-b:a", "160k", c["piece"]])
            c["signals"] = signals(c["piece"], c["start"] - c["p0"], c["end"] - c["start"])
        except Exception as e:  # noqa: BLE001
            log(f"[{kind}] {src['id']}: piece {k} failed ({e})")
            c["signals"] = {"blank_start": True}
        c["score"] = signal_score(c)
    kept = sorted((c for c in cands if c["score"] is not None), key=lambda c: -c["score"])[:KEEP_SIGNAL]
    log(f"[{kind}] {src['id']}: {len(kept)} candidates after the signal pass")
    if not kept:
        return []
    labels = {f"M{i + 1}": c for i, c in enumerate(kept)}
    lines = "\n".join(f"{lab} ({c['end'] - c['start']:.0f} s): \"{c['text'][:500]}\" | angle: "
                      f"{json.dumps(c['angle'], ensure_ascii=False)[:300]}" for lab, c in labels.items())
    rank = ask(llm, settings, RANK.format(**info, lines=lines, keep=per_source + 1),
               [sheet(c["piece"], c["end"] - c["p0"]) for c in kept])
    checks = {x.get("id"): x for x in rank.get("checks") or [] if isinstance(x, dict)}
    order = [lab for lab in rank.get("ranked") or [] if lab in labels]
    order += [lab for lab in labels if lab not in order]
    made = []
    for lab in order:
        if len(made) >= per_source:
            break
        c, chk = labels[lab], checks.get(lab, {})
        c["check"] = chk
        if chk.get("ok") is False or chk.get("era_ok") is False or chk.get("promo_risk") is True:
            log(f"[{kind}] {src['id']} {lab}: dropped in the frame pass ({chk.get('why')})")
            continue
        try:
            m = make_short(src, kind, si, len(made), c, info, settings, llm, t, bare=bare_first and not made)
        except Exception as e:  # noqa: BLE001 - one bad Short must not end the source
            log(f"[{kind}] {src['id']} {lab}: failed ({e})")
            m = None
        if m:
            made.append(m)
    for c in cands:
        Path(c["piece"]).unlink(missing_ok=True)
    return made


def make_short(src, kind, si, k, c, info, settings, llm, t, bare: bool) -> dict | None:
    piece_len = c["end"] - c["p0"]
    sh = sheet(c["piece"], piece_len)
    plan = ask(llm, settings, LAYER_PROMPT.format(**info, secs=c["end"] - c["start"], lines=c["text"][:1500],
                                                  angle=json.dumps(c["angle"], ensure_ascii=False)), [sh])
    if not plan.get("usable", True):
        log(f"[{kind}] {src['id']}: Claude can't add enough of our own to this moment")
        return None
    s0, s1 = c["start"] - c["p0"], c["end"] - c["p0"]
    review = ask(llm, settings, REVIEW_PROMPT.format(**info, lines=c["text"][:1500], hook=plan.get("hook"),
                                                     context=plan.get("context"), payoff=plan.get("payoff")),
                 [grid([("S", frame(c["piece"], s0 + 1.0, 360)), ("E", frame(c["piece"], max(s0, s1 - 1.5), 360))],
                       cols=2)])
    if review.get("ok") is False:
        log(f"[{kind}] {src['id']}: review: payoff can't be made true ({review.get('notes')})")
        return None
    for key in ("hook", "context", "payoff"):
        if review.get(key):
            plan[key] = review[key]
    have = layer_ok(plan)
    if len(have) < 2 or not plan.get("hook"):
        log(f"[{kind}] {src['id']}: our layer has only {have}: no Short (paper 5.4 asks for two)")
        return None
    words = [[w[0], w[1] - c["p0"], w[2] - c["p0"]] for s in t["sentences"][c["from"]:c["to"] + 1] for w in s["words"]]
    rplan = {**plan, "start": s0, "end": s1}
    tight = sum(y - x for x, y in tighten(words, s0, s1)[0])
    if tight < MIN_TIGHT:
        log(f"[{kind}] {src['id']}: only {tight:.0f} s left once the silences are cut: no Short")
        return None
    name = f"{kind}-{si + 1}{chr(97 + k)}"
    lengths = {}
    out = OUT / f"{name}.mp4"
    focus = ask_focus(llm, settings, c["piece"], rplan, words, c["text"]) if FX else None
    lengths[out.name] = round(render(c["piece"], rplan, words, out, focus=focus), 1)
    if bare:
        out_b = OUT / f"{name}-bare.mp4"
        lengths[out_b.name] = round(render(c["piece"], rplan, words, out_b, bare=True), 1)
    log(f"[{kind}] made {', '.join(lengths)} from {int(c['start'] // 60)}:{int(c['start'] % 60):02d} of '{src['title']}'")
    return {"short": name, "kind": kind, "source": src["id"], "link": src.get("link"), "title_source": src["title"],
            "year": src.get("year"), "rights": src.get("rights"), "transcript": t.get("origin"),
            "at": f"{int(c['start'] // 60)}:{int(c['start'] % 60):02d}", "sentences": [c["from"], c["to"]],
            "lengths": lengths, "said": c["text"], "why": c["why"], "scores": c["scores"], "claude": c["claude"],
            "signals": c["signals"], "score": c["score"], "frame_check": c.get("check"), "layer": have,
            "hook": plan.get("hook"), "context": plan.get("context"), "payoff": plan.get("payoff"),
            "yt_title": plan.get("title"), "credit": plan.get("credit"), "review": review.get("notes"),
            "focus": focus}


# ---------- the two steps ----------

def save_json(path: Path, data) -> None:
    def plain(o):  # numpy numbers and bools from the signal pass
        return o.item() if hasattr(o, "item") else str(o)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=plain), encoding="utf-8")


def prep(kind: str, n: int) -> None:
    """Pick n sources of a kind and transcribe them (cached), into sources-<kind>.json."""
    OUT.mkdir(parents=True, exist_ok=True)
    srcs = find_sources(kind, n)
    log(f"[{kind}] {len(srcs)} sources")
    rows = []
    for s in srcs:
        row = dict(s)
        try:
            t = tx.get(s)
            row["transcript"] = {k: t.get(k) for k in ("origin", "model", "audio_s", "run_s", "x_realtime", "took_s",
                                                       "words")}
            row["transcript"]["sentences"] = len(t["sentences"])
            fl = t.get("flags") or {}
            row["transcript"]["repeats_dropped"] = fl.get("repeats", 0)
            row["transcript"]["fillers_dropped"] = len(fl.get("dropped") or []) - fl.get("repeats", 0)
            row["transcript"]["first_lines"] = [x["text"] for x in t["sentences"][:8]]
        except Exception as e:  # noqa: BLE001 - one source failing must not end the part
            log(f"[{kind}] {s['id']}: transcript failed: {e}")
            row["error"] = str(e)[-500:]
        rows.append(row)
        save_json(OUT / f"sources-{kind}.json", rows)
    save_json(OUT / f"sources-{kind}.json", rows)


def make(kind: str, per_source: int, bare_sources: int) -> None:
    from .common import open_library
    path = OUT / f"sources-{kind}.json"
    if not path.exists():
        log(f"[{kind}] no sources yet: run `prep --part {KINDS[kind]['part']}` first")
        return
    srcs = [s for s in json.loads(path.read_text(encoding="utf-8")) if not s.get("error")]
    settings, _conn, llm = open_library()
    for old in OUT.glob(f"{kind}-*.mp4"):
        old.unlink()
    made_path = OUT / "made.json"
    made = [m for m in (json.loads(made_path.read_text(encoding="utf-8")) if made_path.exists() else [])
            if m.get("kind") != kind]
    for si, s in enumerate(srcs):
        log(f"=== [{kind}] {s['id']}: {s['title']}")
        try:
            got = make_source(s, kind, si, settings, llm, per_source, bare_first=si < bare_sources)
        except Exception as e:  # noqa: BLE001
            log(f"[{kind}] {s['id']} failed: {e}")
            got = []
        made += got or [{"kind": kind, "source": s["id"], "title_source": s["title"], "failed": True}]
        save_json(made_path, made)
    ok = sum(1 for m in made if m.get("kind") == kind and not m.get("failed"))
    log(f"[{kind}] done: {ok} Shorts from {len(srcs)} sources")


def share() -> list[Path]:
    """Fresh copies of every sample Short in share/, so the runner publishes previews of all of
    them in one job (it only keeps previews of the last 5 jobs, and only of new files)."""
    dest = OUT / "share"
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.mp4"):
        old.unlink()
    out = []
    for f in sorted(OUT.glob("*.mp4")):
        (dest / f.name).write_bytes(f.read_bytes())
        out.append(dest / f.name)
    log(f"shared {len(out)} Shorts: {', '.join(f.name for f in out)}")
    return out


def looks(names: list[str] | None = None) -> list[Path]:
    """Roeland's look test: the Shorts in config/look-test.toml, re-cut from their sources with the
    texts listed there, once in every theme of TEST_THEMES. Into looks/<short>-<theme>.mp4."""
    from shorts import media
    test = tomllib.loads(LOOK_TEST.read_text(encoding="utf-8"))["short"]
    made = {m["short"]: m for m in json.loads((OUT / "made.json").read_text(encoding="utf-8")) if m.get("short")}
    srcs = {s["id"]: s for f in OUT.glob("sources-*.json") for s in json.loads(f.read_text(encoding="utf-8"))}
    dest, work = OUT / "looks", OUT / "work"
    dest.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.mp4"):
        old.unlink()
    out = []
    for row in test:
        name = row["name"]
        if names and name not in names:
            continue
        m = made.get(name)
        src = srcs.get(m["source"]) if m else None
        if not src:
            log(f"[looks] {name}: not in made.json or its source is gone, skipped")
            continue
        sents = tx.get(src)["sentences"]
        c = snap(sents, *m["sentences"])
        if not c:
            log(f"[looks] {name}: sentences {m['sentences']} no longer make a Short, skipped")
            continue
        p0 = max(0.0, c["start"] - 1.0)
        piece = work / f"looks-{name}.mp4"
        media.run(["-ss", f"{p0:.2f}", "-t", f"{c['end'] - p0 + 1.0:.2f}", "-i", src["url"],
                   "-vf", "scale=-2:min(720\\,ih)", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                   "-c:a", "aac", "-b:a", "160k", str(piece)])
        words = [[w[0], w[1] - p0, w[2] - p0] for s in sents[c["from"]:c["to"] + 1] for w in s["words"]]
        plan = {**{k: m.get(k) for k in ("hook", "context", "payoff")},  # a row without texts uses the made ones
                **{k: row[k] for k in ("hook", "context", "payoff") if row.get(k)},
                "credit": m.get("credit", ""), "start": c["start"] - p0, "end": c["end"] - p0}
        for theme in TEST_THEMES:
            f = dest / f"{name}-{theme}.mp4"
            render(str(piece), plan, words, f, theme=theme)
            out.append(f)
        piece.unlink(missing_ok=True)
        log(f"[looks] {name}: {len(TEST_THEMES)} themes made")
    return out


# Roeland 2026-11-13: compare only 5 Shorts, as different as possible (one per kind, two newsreels).
COMPARE = ("newsreel-1a", "newsreel-2a", "prelinger-1a", "nasa-2a", "feature-2a")


def compare(names: list[str] | None = None) -> list[Path]:
    """Every made Short twice, from the same cut and texts: <short>-plain.mp4 without the engagement
    elements and <short>-fx.mp4 with them, into compare/ (Roeland's side-by-side, 2026-11-13)."""
    from shorts import media
    from .common import open_library
    settings, _conn, llm = open_library()
    made = [m for m in json.loads((OUT / "made.json").read_text(encoding="utf-8")) if m.get("short")]
    srcs = {s["id"]: s for f in OUT.glob("sources-*.json") for s in json.loads(f.read_text(encoding="utf-8"))}
    dest, work = OUT / "compare", OUT / "work"
    dest.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.mp4"):
        old.unlink()
    names = list(names or COMPARE)
    out, rows = [], []
    for m in made:
        name = m["short"]
        src = srcs.get(m["source"])
        if (names and name not in names) or not src:
            continue
        try:
            sents = tx.get(src)["sentences"]
            c = snap(sents, *m["sentences"])
            if not c:
                log(f"[compare] {name}: no longer makes a Short, skipped")
                continue
            p0 = max(0.0, c["start"] - 1.0)
            piece = work / f"compare-{name}.mp4"
            media.run(["-ss", f"{p0:.2f}", "-t", f"{c['end'] - p0 + 1.0:.2f}", "-i", src["url"],
                       "-vf", "scale=-2:min(720\\,ih)", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                       "-c:a", "aac", "-b:a", "160k", str(piece)])
            words = [[w[0], w[1] - p0, w[2] - p0] for s in sents[c["from"]:c["to"] + 1] for w in s["words"]]
            plan = {**{k: m.get(k) for k in ("hook", "context", "payoff", "credit")},
                    "start": c["start"] - p0, "end": c["end"] - p0}
            plain = render(str(piece), plan, words, dest / f"{name}-plain.mp4", fx=False)
            focus = ask_focus(llm, settings, str(piece), plan, words, m.get("said", ""))
            fxd = render(str(piece), plan, words, dest / f"{name}-fx.mp4", fx=True, focus=focus)
            piece.unlink(missing_ok=True)
            out += [dest / f"{name}-plain.mp4", dest / f"{name}-fx.mp4"]
            rows.append({"short": name, "plain_s": plain, "fx_s": fxd, "focus": focus})
            log(f"[compare] {name}: plain {plain:.1f} s, with elements {fxd:.1f} s, circle: {bool(focus)}")
        except Exception as e:  # noqa: BLE001 - one Short failing must not end the job
            log(f"[compare] {name} failed: {e}")
        save_json(OUT / "compare.json", rows)
    return out


BATCH = 20                                  # Shorts per upload batch (Roeland 2026-11-13: 20 on historybuffed)
BATCH_PER_SOURCE = 3
UPLOAD_CHANNEL = "historybuffed"
UPLOAD_DIR = OUT / "upload"
PLAN = OUT / "upload-plan.json"
FORMAT_ID = "tclips"                        # shorts.format_id of the transcript-first Shorts in the live database
CATEGORY = "27"                             # Education
# Checked by hand before the upload (2026-11-13): out of the plan, and the spares that take their place.
DROP = {"feature-3c": "payoff error: Myrtle Vail was writer Charles Griffith's grandmother, not his mother"}
SPARES = ("feature-2b",)


def checked_plan(plan: list[dict]) -> list[dict]:
    """The plan without DROP, plus SPARES (from made.json, their MP4 still in OUT) at the end."""
    out = [m for m in plan if m["short"] not in DROP]
    have = {m["short"] for m in out}
    made_path = OUT / "made.json"
    made = {m["short"]: m for m in (json.loads(made_path.read_text(encoding="utf-8")) if made_path.exists() else [])
            if m.get("short")}
    for name in SPARES:
        m, path = made.get(name), OUT / f"{name}.mp4"
        if name not in have and m and path.exists():
            out.append({**m, "path": str(path), "secs": m["lengths"].get(path.name)})
    return out


def spread(rows: list[dict]) -> list[dict]:
    """Best first, but never two Shorts from the same film on consecutive days when it can be helped."""
    left, out = list(rows), []
    while left:
        pick = next((r for r in left if not out or r["source"] != out[-1]["source"]), left[0])
        left.remove(pick)
        out.append(pick)
    return out


def batch(n: int = BATCH) -> list[dict]:
    """Fresh Shorts from every prepared source with today's defaults (ragebait texts, Tabloid, silences
    cut, no effects), then the best n move to upload/<date>/ and go into upload-plan.json in posting order."""
    for kind in KINDS:
        if (OUT / f"sources-{kind}.json").exists():
            make(kind, BATCH_PER_SOURCE, 0)
    made = [m for m in json.loads((OUT / "made.json").read_text(encoding="utf-8"))
            if m.get("short") and not m.get("failed") and (OUT / f"{m['short']}.mp4").exists()]
    best = spread(sorted(made, key=lambda m: -(m.get("score") or 0))[:n])
    dest = UPLOAD_DIR / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest.mkdir(parents=True, exist_ok=True)
    plan = []
    for m in best:
        path = dest / f"{m['short']}.mp4"
        shutil.move(str(OUT / f"{m['short']}.mp4"), path)
        plan.append({**m, "path": str(path), "secs": m["lengths"].get(f"{m['short']}.mp4")})
    save_json(PLAN, plan)
    log(f"[batch] {len(made)} Shorts made, the best {len(plan)} in {dest}")
    return plan


def upload_meta(m: dict, channel, settings) -> dict:
    """Title, description and tags for one transcript-first Short."""
    from shorts.render import all_tags, clean_text, taglayers
    year = m.get("year")
    post = [KINDS[m["kind"]]["label"], str(year) if year else "history", "archive footage"]
    nine = all_tags(post, channel, settings)
    src = m.get("credit") or m.get("title_source") or ""
    lines = [m.get("context") or "", "",
             f"Footage: {src}" + (f" ({year})" if year and str(year) not in src else ""),
             m.get("link") or ""]
    if m["kind"] == "nasa":
        lines += ["", "NASA does not endorse this channel."]
    lines += ["", " ".join(dict.fromkeys(taglayers.hashtag(t) for t in nine))]
    title = clean_text(m.get("yt_title") or m.get("hook") or "").strip()
    return {"title": title[:100], "description": clean_text("\n".join(lines).strip())[:4900],
            "tags": [clean_text(t) for t in nine if clean_text(t).strip()], "categoryId": CATEGORY,
            "containsSyntheticMedia": False}


def next_day(conn, channel, now: datetime | None = None) -> datetime:
    """One transcript-first Short per day: the day after the last one planned (tomorrow at the
    earliest), at the channel's first publish time, an hour later while another Short has that time."""
    from shorts.upload import iso
    now = now or datetime.now(timezone.utc)
    last = conn.execute("SELECT MAX(publish_at) FROM shorts WHERE channel=? AND format_id=? AND publish_at IS NOT NULL",
                        (channel.id, FORMAT_ID)).fetchone()[0]
    day = (now + timedelta(days=1)).date()
    if last:
        day = max(day, datetime.fromisoformat(last.replace("Z", "+00:00")).date() + timedelta(days=1))
    taken = {r[0] for r in conn.execute("SELECT publish_at FROM shorts WHERE channel=? AND publish_at IS NOT NULL",
                                        (channel.id,))}
    first = sorted(channel.publish_times_utc or ["15:00"])[0]
    slot = datetime.combine(day, datetime.strptime(first, "%H:%M").time(), tzinfo=timezone.utc)
    while iso(slot) in taken:
        slot += timedelta(hours=1)
    return slot


def upload(channel_id: str = UPLOAD_CHANNEL) -> list[str]:
    """Upload upload-plan.json to the channel as private Shorts with a publish time, one per day.
    Writes a row per Short into the live database (format_id 'tclips') so the dashboard schedule and
    stats see them and the normal daily upload never picks them. Stays within the channel's uploads
    per 24 hours and the shared daily quota; run it again later for the rest (done ones are skipped)."""
    from googleapiclient.http import MediaFileUpload

    from shorts import db as sdb
    from shorts import upload as up
    from shorts.config import load_channel
    from .common import open_library
    settings, conn, _llm = open_library()
    ch = load_channel(channel_id, settings.config_dir)
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM shorts WHERE channel=? GROUP BY status", (ch.id,)).fetchall())
    log(f"[upload] '{ch.id}' ({ch.name}) in the database now: {counts}; publish times {ch.publish_times_utc}, "
        f"{ch.uploads_per_day} per day for its own Shorts, paused={ch.paused}, via={ch.upload_via}")
    plan = checked_plan(json.loads(PLAN.read_text(encoding="utf-8")) if PLAN.exists() else [])
    done = {r[0] for r in conn.execute(
        "SELECT video_path FROM shorts WHERE channel=? AND format_id=? AND status IN ('scheduled', 'uploaded')",
        (ch.id, FORMAT_ID))}
    todo = [m for m in plan if m["path"] not in done and Path(m["path"]).exists()]
    if (ch.upload_via or "api") != "api":
        log(f"[upload] '{ch.id}' uploads via the phone, not the API: nothing uploaded")
        return []
    until = up.blocked_until(settings, ch)
    if until:
        log(f"[upload] YouTube refused uploads for '{ch.id}' recently; try again after {up.iso(until)}")
        return []
    room = min(settings.channel_uploads_per_day - up.channel_uploads_last_day(conn, ch),
               settings.upload_quota - up.uploads_today(conn))
    log(f"[upload] {len(plan)} in the plan, {len(done)} already up, {len(todo)} to go, room for {max(room, 0)} now")
    if not todo or room <= 0:
        return []
    yt = up.youtube(settings, ch, interactive=False)
    up.check_channel(yt, ch)
    out = []
    for m in todo[:room]:
        meta = upload_meta(m, ch, settings)
        slot = next_day(conn, ch)
        cur = conn.execute(
            """INSERT INTO shorts (channel, format_id, topic, hook, title, description, tags, status, video_path,
                                   duration) VALUES (?, ?, ?, ?, ?, ?, ?, 'rendered', ?, ?)""",
            (ch.id, FORMAT_ID, m.get("why"), m.get("hook"), meta["title"], meta["description"],
             json.dumps(meta["tags"]), m["path"], m.get("secs")))
        conn.commit()
        sid = cur.lastrowid
        try:
            req = yt.videos().insert(part="snippet,status", body=up.body_for(meta, slot),
                                     media_body=MediaFileUpload(m["path"], mimetype="video/mp4", chunksize=8 << 20,
                                                                resumable=True))
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
        except Exception as e:  # noqa: BLE001
            sdb.set_short(conn, sid, status="failed", error=str(e)[:500])
            reason = up._limit_reason(e)
            if reason == "uploadLimitExceeded":
                up._blocked_path(settings, ch).write_text(up.iso(datetime.now(timezone.utc)), encoding="utf-8")
            log(f"[upload] {m['short']} failed ({reason or e}); stopped after {len(out)}")
            break
        sdb.set_short(conn, sid, status="scheduled", youtube_id=resp["id"], publish_at=up.iso(slot),
                      uploaded_at=up.iso(datetime.now(timezone.utc)))
        out.append(resp["id"])
        log(f"[upload] {m['short']} -> https://youtu.be/{resp['id']} publishes {up.iso(slot)}: {meta['title']}")
    save_json(OUT / "upload-done.json", [dict(r) for r in conn.execute(
        "SELECT id, title, youtube_id, publish_at, status, video_path FROM shorts WHERE channel=? AND format_id=? "
        "ORDER BY publish_at", (ch.id, FORMAT_ID))])
    log(f"[upload] {len(out)} uploaded now, {len(todo) - len(out)} still to go")
    return out


def unpublished(yt) -> list[dict]:
    """Every video on the logged-in channel that is not public yet: private, with or without a publish time."""
    ch = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    playlist = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, page = [], None
    while True:
        r = yt.playlistItems().list(part="contentDetails", playlistId=playlist, maxResults=50, pageToken=page).execute()
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        page = r.get("nextPageToken")
        if not page:
            break
    out = []
    for k in range(0, len(ids), 50):
        r = yt.videos().list(part="status,snippet", id=",".join(ids[k:k + 50]), maxResults=50).execute()
        for v in r.get("items", []):
            st = v.get("status", {})
            if st.get("privacyStatus") == "private":
                out.append({"id": v["id"], "title": v["snippet"].get("title"), "publish_at": st.get("publishAt"),
                            "uploaded": v["snippet"].get("publishedAt")})
    return sorted(out, key=lambda v: v.get("publish_at") or "")


def clear(channel_id: str = UPLOAD_CHANNEL, delete: bool = False) -> list[dict]:
    """Roeland 2026-11-13: remove every Short on the channel that is not public yet, so the
    transcript-first batch starts tomorrow. Without delete it only lists them (clear-list.json).
    With delete: deletes those videos on YouTube (never the transcript-first ones), marks their rows
    rejected, and rejects the channel's approved Shorts so the daily upload adds no old ones."""
    from shorts import upload as up
    from shorts.config import load_channel
    from .common import open_library
    settings, conn, _llm = open_library()
    ch = load_channel(channel_id, settings.config_dir)
    yt = up.youtube(settings, ch, interactive=False)
    up.check_channel(yt, ch)
    ours = {r[0] for r in conn.execute("SELECT youtube_id FROM shorts WHERE channel=? AND format_id=? AND youtube_id "
                                       "IS NOT NULL", (ch.id, FORMAT_ID))}
    vids = [v for v in unpublished(yt) if v["id"] not in ours]
    approved = [dict(r) for r in conn.execute("SELECT id, title FROM shorts WHERE channel=? AND status='approved' "
                                              "AND format_id != ?", (ch.id, FORMAT_ID))]
    log(f"[clear] '{ch.id}': {len(vids)} unpublished videos on YouTube, {len(approved)} approved and not uploaded")
    for v in vids:
        log(f"[clear]   {v['id']} publish {v['publish_at'] or 'never (private)'}: {v['title']}")
    for a in approved:
        log(f"[clear]   approved #{a['id']}: {a['title']}")
    save_json(OUT / "clear-list.json", {"channel": ch.id, "videos": vids, "approved": approved,
                                        "can_delete": up.can_manage(settings, ch)})
    if not delete:
        return vids
    conn.execute("UPDATE shorts SET status='rejected', reject_reason=?, updated_at=datetime('now') "
                 "WHERE channel=? AND status='approved' AND format_id != ?",
                 ("not uploaded (Roeland 2026-11-13: transcript-first Shorts instead)", ch.id, FORMAT_ID))
    conn.commit()
    log(f"[clear] {len(approved)} approved Shorts set to rejected: the daily upload adds none of them")
    if not up.can_manage(settings, ch):
        log(f"[clear] the saved YouTube login of '{ch.id}' may upload but not delete videos: nothing deleted. "
            "Connect YouTube again with permission to manage videos.")
        return []
    gone = []
    for v in vids:
        try:
            yt.videos().delete(id=v["id"]).execute()
        except Exception as e:  # noqa: BLE001
            log(f"[clear] {v['id']} not deleted: {e}")
            continue
        gone.append(v)
        conn.execute("UPDATE shorts SET status='rejected', reject_reason=?, updated_at=datetime('now') "
                     "WHERE channel=? AND youtube_id=?",
                     ("deleted before publishing (Roeland 2026-11-13: transcript-first Shorts instead)", ch.id, v["id"]))
        log(f"[clear] deleted {v['id']}: {v['title']}")
    save_json(OUT / "clear-done.json", gone)
    log(f"[clear] {len(gone)} of {len(vids)} deleted, {len(approved)} approved Shorts set to rejected")
    return gone


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["prep", "make", "share", "looks", "compare", "batch", "upload", "clear", "clearnow"])
    ap.add_argument("--part", type=int, choices=sorted(PARTS))
    ap.add_argument("--sources", type=int, default=3)
    ap.add_argument("--per-source", type=int, default=2)
    ap.add_argument("--bare", type=int, default=2, help="sources whose first Short also gets a bare version")
    a = ap.parse_args(argv)
    if a.step == "share":
        share()
        return
    if a.step == "looks":
        looks()
        return
    if a.step == "compare":
        compare()
        return
    if a.step in ("clear", "clearnow"):
        clear(delete=a.step == "clearnow")
        return
    if a.step in ("batch", "upload"):
        batch() if a.step == "batch" else upload()
        return
    if a.part is None:
        ap.error("--part is needed for prep and make")
    kind = PARTS[a.part]
    if a.step == "prep":
        prep(kind, a.sources)
    else:
        make(kind, a.per_source, a.bare)


if __name__ == "__main__":
    main()
