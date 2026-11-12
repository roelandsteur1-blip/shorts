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

Samples only: MP4s and made.json in ~/meta-out/tclips/. Nothing is uploaded; the `shorts`
database is not touched.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path
from statistics import mean
from urllib.parse import quote

from PIL import Image

from . import transcript as tx
from .clips import PD, UA, build_band, cuts, frame, grid, pick_file, probe_length, search, text_block, year_of
from .textcard import H, W, black_bars, log

OUT = Path.home() / "meta-out" / "tclips"
TITLES = Path(__file__).resolve().parents[1] / "config" / "clip-titles.toml"
SUB_EXT = (".srt", ".vtt")

MIN_LEN, MAX_LEN, CAP = 15.0, 45.0, 58.0   # paper 7.2/8: 15-45 s, never over 60 s (Content ID)
AIR = 0.3                                   # seconds of air before the first and after the last word
KEEP_SIGNAL = 5                             # candidates that go on to the frame pass
LAYER = ("context", "verdict", "payoff_fact", "story_frame", "comparison", "commentary")
BLOCK = ("killed", "kill", "dead", "death", "deaths", "died", "dying", "corpse", "corpses", "murder",
         "murdered", "suicide", "massacre", "executed", "execution", "lynched", "slaughter")

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
SOUND. Read whole stretches, don't score single lines: a moment is usually 3-12 sentences. Score
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

YouTube does NOT count captions of the dialogue, or text that only describes what is on screen, as
original. Our layer has to ADD something the viewer can't get from the clip. Write:
- "hook": on screen above the film the whole time. Max 9 words, curiosity first, no emoji.
- "context": shown during the first 5 seconds. Max 14 words: who, when, or why it matters. Not a
  description of what is on screen.
- "payoff": shown at the end. Max 22 words: a TRUE fact that goes beyond the clip (what happened
  next, what was later proven wrong), or a verdict with its reason. Only facts you are sure of;
  never invent names, dates or numbers.
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

def subtitle_blocks(words: list[list], start: float, end: float, max_words: int = 6) -> list[tuple[float, float, str]]:
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
            out.append((round(t0, 2), round(t1, 2), " ".join(str(w[0]) for w in b)))
    return out


def render(piece: str, plan: dict, words: list[list], out: Path, bare: bool = False) -> float:
    """One Short from a local piece of the source (times in piece time). bare = the same cut with
    subtitles only, for the side-by-side test of what our layer adds."""
    from shorts import media
    start, end = plan["start"], plan["end"]
    total = end - start
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
        band = build_band(piece, [{"kind": "play", "t0": start, "t1": end, "speed": 1.0}], crop, band_h, True, tmp)
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if not bare and plan.get("hook"):
            hook = text_block(plan["hook"], 70, "white")
            base.alpha_composite(hook, (0, max(150, band_y - hook.height - 50)))
        credit = text_block(plan.get("credit", ""), 30, (170, 170, 170))
        base.alpha_composite(credit, (0, H - 200))
        base.save(tmp / "base.png")
        inputs = ["-f", "lavfi", "-t", f"{total:.2f}", "-i", f"color=black:s={W}x{H}:r=30",
                  "-i", str(band), "-loop", "1", "-t", f"{total:.2f}", "-i", str(tmp / "base.png")]
        fc = f"[0:v][1:v]overlay=0:{band_y}[a0];[a0][2:v]overlay=0:0[b0]"
        last, idx = "b0", 3
        overlays = []  # (png, x, y, t0, t1)
        for i, (t0, t1, text) in enumerate(subtitle_blocks(words, start, end)):
            im = text_block(text, 46, "white", width=W - 160)
            png = tmp / f"s{i}.png"
            im.save(png)
            overlays.append((png, band_y + band_h - im.height - 6, t0, t1))
        if not bare:
            ctx_end = min(5.0, total * 0.3)
            pay_at = max(total - 5.0, total * 0.65)
            for name, text, t0, t1, colour in (("ctx", plan.get("context"), 0.0, ctx_end, "white"),
                                               ("pay", plan.get("payoff"), pay_at, total + 1, (255, 221, 0))):
                if text:
                    png = tmp / f"{name}.png"
                    text_block(text, 46, colour, box=True).save(png)
                    overlays.append((png, band_y + band_h + 50, t0, t1))
        for png, y, t0, t1 in overlays:
            inputs += ["-loop", "1", "-t", f"{total:.2f}", "-i", str(png)]
            fc += f";[{last}][{idx}:v]overlay=0:{y}:enable='between(t,{t0:.2f},{t1:.2f})'[o{idx}]"
            last, idx = f"o{idx}", idx + 1
        fc += f";[{last}]format=yuv420p[out];[1:a]loudnorm=I=-16:TP=-1.5,aresample=48000[aud]"
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
    name = f"{kind}-{si + 1}{chr(97 + k)}"
    lengths = {}
    out = OUT / f"{name}.mp4"
    lengths[out.name] = round(render(c["piece"], rplan, words, out), 1)
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
            "yt_title": plan.get("title"), "credit": plan.get("credit"), "review": review.get("notes")}


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


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["prep", "make"])
    ap.add_argument("--part", type=int, choices=sorted(PARTS), required=True)
    ap.add_argument("--sources", type=int, default=3)
    ap.add_argument("--per-source", type=int, default=2)
    ap.add_argument("--bare", type=int, default=2, help="sources whose first Short also gets a bare version")
    a = ap.parse_args(argv)
    kind = PARTS[a.part]
    if a.step == "prep":
        prep(kind, a.sources)
    else:
        make(kind, a.per_source, a.bare)


if __name__ == "__main__":
    main()
