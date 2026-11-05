"""The footage collector: topic first. Given a subject ("Saturn V", "Spitfire"), find ONE clip that
really shows it, for every Short type (research: meta/collector/, report "Footage collector
research: final report", 5 November 2026).

1. Library first: today's footage library, searched for the subject's name. Costs seconds.
2. Only when nothing there passes: the archives the channel ticked in its Source test
   (shorts/sourcetest.py), best thumbnails first (PE-Core), fetching only the piece we need
   (small files whole, big ones cut remotely, photos as a slow zoom), 3 at a time.
3. The check on every clip: no very dark clips (except space), no text-heavy ones (when RapidOCR
   is installed), then Claude's blind check on 3 frames ("does this show X?": 92% good by
   Roeland's eye in round 2). No BRISQUE or sharpness score: they didn't predict his marks.
4. For old-footage niches (old cities, history, retro tech) an old-looking clip wins.

A clip from an archive is added to the library (asset + one scene, with its licence and credit),
so the rest of the program uses it like any other footage: render, credits, "used" checks.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, llm
from .config import Channel, Settings

log = logging.getLogger(__name__)

AGE_CUT = 0.02                       # old_look above this = old film (round 2, Roeland's oldcity check)
OLD_NICHES = {"oldcity", "history", "retrotech"}
PARALLEL = 3                         # archive pieces fetched (and checked) at a time
LIBRARY_TRIES = 4                    # library clips checked before going to the archives
REJECTS = {"black", "text on screen"}  # the only gates that predicted bad clips (round 2)
# Source test columns -> what the collector searches, and how many hits of each it may fetch.
COLUMNS = {
    "Commons video (name)": ("commons_video", 3),
    "Commons video (depicts)": ("commons_depicts", 2),
    "NASA video": ("nasa", 2),
    "Pixabay (B-roll)": ("pixabay", 1),
    "Commons photos (depicts)": ("commons_photo", 2),
    "Openverse photos": ("openverse", 2),
}
VIDEO_FIRST = ("commons_video", "commons_depicts", "nasa", "pixabay")
LIBRARY_SOURCE = {"commons_video": "commons", "commons_depicts": "commons", "commons_photo": "commons",
                  "nasa": "nasa", "pixabay": "pixabay", "openverse": "openverse"}
WHERE = {"commons": "Wikimedia Commons", "openverse": "Openverse"}


class Collector:
    """One per channel and run. find() returns a library scene id that shows the subject."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings, channel: Channel, verify=None, ask_json=None):
        from . import metatype, sourcetest
        self.conn, self.settings, self.channel = conn, settings, channel
        self.niche = metatype.niche_for(channel) or channel.id
        columns = sourcetest.picked(channel)
        self.use_library = sourcetest.LIBRARY in columns
        self.archives = [COLUMNS[c] for c in columns if c in COLUMNS]
        self.used: set[int] = set(db.used_scene_ids(conn, channel.id))
        self._verify = verify
        self._ask = ask_json or (lambda prompt: llm.ask_json(prompt, settings.claude_cmd, retries=1))
        self._tools = None
        self.log: list[dict] = []  # one record per subject: where its clip came from, how long it took

    # ---------- shared tools (loaded once, on first use) ----------

    def tools(self):
        if self._tools is None:
            _research_tools()
            from meta.collector import pieces, sweep
            scorer = sweep.Scorer(self.settings)
            self._tools = (scorer, pieces.Gates(scorer, brisque=False), sweep.load_keys(self.settings))
        return self._tools

    def verify(self, items: list[tuple[str, bytes, str]]) -> set[str]:
        if self._verify:
            return self._verify(items)
        from meta.common import verify
        return verify(llm, self.settings, items)

    # ---------- the collector ----------

    def find(self, name: str, show: str | None = None, need: float = 8.0) -> int | None:
        """A scene id of at least `need` seconds that shows `name`, or None. The scene counts as
        used from now on (a second find() never returns it)."""
        subject = {"niche": self.niche, "name": name, "wikidata": name, "show": show or f"a clear view of {name}"}
        rec = {"name": name, "library": 0, "archive": 0}
        t0 = time.time()
        sid = None
        try:
            if self.use_library:
                sid = self._from_library(subject, need, rec)
            if sid is None and self.archives:
                sid = self._from_archives(subject, need, rec)
        except llm.LLMLimitError:
            raise
        except Exception as e:  # noqa: BLE001 - one subject must never stop a Short or a run
            log.warning("collector: %s failed: %s", name, e)
            rec["error"] = str(e)[:200]
        rec.update(scene=sid, seconds=round(time.time() - t0, 1))
        self.log.append(rec)
        if sid is not None:
            self.used.add(sid)
        log.info("collector: %s -> %s (%.0f s)", name, f"scene {sid} from {rec.get('from')}" if sid else "nothing",
                 rec["seconds"])
        return sid

    def _from_library(self, subject: dict, need: float, rec: dict) -> int | None:
        from meta.collector import pieces
        from meta.textcard import find_clips
        blocked = self._blocked_assets()
        clips = [c for c in find_clips(self.conn, self.settings, [subject["name"]], self.used, need,
                                       per_query=LIBRARY_TRIES + 2)
                 if self._asset_of(c["id"]) not in blocked][:LIBRARY_TRIES]
        if not clips:
            return None
        _, gates, _ = self.tools()
        made = []
        for i, c in enumerate(clips):
            frames = [f for f in (pieces.frame_at(c["path"], c["start"] + t, 640)
                                  for t in (0.4, need / 2, max(0.5, need - 0.4))) if f]
            if not frames:
                continue
            made.append({"label": f"L{i}", "scene": c["id"], "gates": gates.check(frames),
                         "strip": pieces.strip(frames)})
        rec["library"] = len(made)
        ok = self.verify([(m["label"], m["strip"], subject["name"]) for m in made]) if made else set()
        pick = choose(made, ok, self.niche)
        if pick:
            rec["from"] = "library"
            return pick["scene"]
        return None

    def _blocked_assets(self) -> set[int]:
        """Videos another channel opened a Short with, or that were judged off-niche for this one."""
        from . import hero
        return hero.other_channels_videos(self.conn, self.channel.id) | {
            r[0] for r in self.conn.execute("SELECT asset_id FROM channel_fit WHERE channel=? AND fits=0",
                                            (self.channel.id,))}

    def _asset_of(self, scene_id: int) -> int | None:
        r = self.conn.execute("SELECT asset_id FROM scenes WHERE id=?", (scene_id,)).fetchone()
        return r[0] if r else None

    def _from_archives(self, subject: dict, need: float, rec: dict) -> int | None:
        from meta.collector import pieces
        scorer, gates, keys = self.tools()
        if {"commons_photo", "commons_depicts"} & {s for s, _ in self.archives}:
            subject["qid"] = self._qid(subject)
        cands = [c for c in pieces.candidates(subject, None, scorer, keys, sources=self.archives) if "error" not in c]
        cands.sort(key=lambda c: (c["source"] not in VIDEO_FIRST, -(c.get("thumb_score") or 0)))
        if not cands:
            return None
        for i, c in enumerate(cands):
            c["label"] = f"K{i}"
        with tempfile.TemporaryDirectory(prefix="collector-") as tmpd:
            tmp = Path(tmpd)
            made: list[dict] = []
            ok: set[str] = set()
            for at in range(0, len(cands), PARALLEL):
                batch = cands[at:at + PARALLEL]
                with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
                    got = [m for m in pool.map(lambda c: self._fetch(c, subject, need, tmp), batch) if m]
                rec["archive"] += len(got)
                made += got
                if got:
                    ok |= self.verify([(m["label"], m["strip"], subject["name"]) for m in got])
                pick = choose(made, ok, self.niche)
                if pick:
                    rec["from"] = pick["source"]
                    return add_to_library(self.conn, self.settings, self.channel, pick, subject)
        return None

    def _fetch(self, c: dict, subject: dict, need: float, tmp: Path) -> dict | None:
        from meta.collector import pieces
        scorer, gates, _ = self.tools()
        try:
            path = (pieces.fetch_photo(c, tmp, seconds=need) if c.get("kind") == "photo"
                    else pieces.fetch_video(c, subject, scorer, tmp, seconds=need))
            frames = pieces.clip_frames(path, pieces.duration(str(path)) or need)
            if not frames:
                return None
            return dict(c, path=str(path), gates=gates.check(frames), strip=pieces.strip(frames))
        except Exception as e:  # noqa: BLE001 - next candidate
            log.info("collector: %s %s failed: %s", c["source"], c.get("title", "")[:60], str(e)[:160])
            return None

    def _qid(self, subject: dict) -> str | None:
        from meta.collector import sweep
        try:
            matches = sweep.wikidata_matches(subject["wikidata"])
            return sweep.claude_qids([subject], [matches], self._ask).get(0)
        except Exception as e:  # noqa: BLE001 - the "depicts" searches are skipped
            log.info("collector: no Wikidata item for %s: %s", subject["name"], e)
            return None


def _research_tools() -> None:
    """The text gate needs RapidOCR, installed in the research venv (meta/collector/tools.py), not
    in the live one: add that venv's packages at the END of the path, so live packages still win."""
    import importlib.util
    import sys
    if importlib.util.find_spec("rapidocr_onnxruntime"):
        return
    for site in sorted((Path.home() / "research-venv" / "lib").glob("python*/site-packages")):
        if str(site) not in sys.path:
            sys.path.append(str(site))


def choose(made: list[dict], ok: set[str], niche: str) -> dict | None:
    """The clip to use: passed Claude's check and the gates (dark is fine in space); for old-footage
    niches an old-looking clip first, else the first in order."""
    rejects = REJECTS - ({"black"} if niche == "space" else set())
    good = [m for m in made if m["label"] in ok and not (rejects & set(m["gates"].get("faults", [])))]
    if niche in OLD_NICHES:
        old = [m for m in good if (m["gates"].get("old_look") or 0) > AGE_CUT]
        good = old + [m for m in good if m not in old]
    return good[0] if good else None


# ---------- archive pieces into the library ----------

def rights(c: dict) -> tuple[str, str | None]:
    """(licence, attribution) of an archive hit. CC BY needs a credit line."""
    from .sources import Rights, cc_by_attribution
    src = LIBRARY_SOURCE.get(c["source"], c["source"])
    if src == "nasa":
        return "public-domain", None
    if src == "pixabay":
        return "Pixabay Content License", None
    lic = str(c.get("licence") or "").strip().lower()
    if lic.startswith("cc0"):
        return "CC0", None
    if lic.startswith("pdm") or "public" in lic:
        return "public-domain", None
    m = re.match(r"(?:cc[ -])?by[ -]?(\d\.\d)?$", lic)
    if m:
        r = cc_by_attribution(Rights(license=f"CC BY {m.group(1) or ''}".strip()), c.get("creator") or "",
                              c.get("provider") or WHERE.get(src, src))
        return r.license, r.attribution
    return str(c.get("licence") or "unknown"), None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "clip"


def add_to_library(conn: sqlite3.Connection, settings: Settings, channel: Channel, c: dict, subject: dict) -> int:
    """Store a fetched piece as a library asset with one scene; returns the scene id."""
    from meta.collector import pieces
    from . import fit, media
    src = LIBRARY_SOURCE.get(c["source"], c["source"])
    folder = settings.footage_dir / "collector"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    dst = folder / f"{_slug(subject['name'])}-{stamp}.mp4"
    shutil.copy(c["path"], dst)
    info = media.probe(dst)
    licence, attribution = rights(c)
    ref = c.get("ref") or c.get("file") or str(stamp)
    at = (c.get("fetch") or {}).get("at")
    cur = conn.execute(
        """INSERT INTO assets (source, source_id, title, description, keywords, credit, license, attribution,
                               needs_check, source_url, file_url, path, duration, width, height, status)
           VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,'scened')""",
        (src, f"{ref}#{at if at is not None else 0}@{stamp}", c.get("title") or subject["name"],
         f"Shows: {subject['name']} (found for this subject and checked by eye).", subject["name"],
         c.get("creator") or None, licence, attribution, c.get("ref"), c.get("file"), str(dst),
         info["duration"], info["width"], info["height"]))
    asset_id = cur.lastrowid
    kf_dir = settings.keyframe_dir / "collector"
    kf_dir.mkdir(parents=True, exist_ok=True)
    kf = kf_dir / f"{asset_id}.jpg"
    frame = pieces.frame_at(str(dst), info["duration"] / 2, 384)
    if frame:
        frame.save(kf, quality=90)
    g = c.get("gates") or {}
    cur = conn.execute(
        """INSERT INTO scenes (asset_id, start, end, keyframe, motion, contrast, brightness, text_like, crop_x)
           VALUES (?, 0, ?, ?, ?, NULL, ?, ?, 0.5)""",
        (asset_id, info["duration"], str(kf) if frame else None,
         (g.get("motion") or 0) / 255, (g.get("brightness") or 0) / 255, g.get("text_share")))
    scene_id = cur.lastrowid
    conn.commit()
    fit.mark(conn, channel, asset_id, True, f"collector: {subject['name']}", method="collector")
    if frame:
        try:
            from . import embed
            embed.embed_pending(conn, settings)
        except Exception as e:  # noqa: BLE001 - the scene still renders; it just isn't searchable yet
            log.warning("collector: scene %d not embedded: %s", scene_id, e)
    return scene_id


def clip(conn: sqlite3.Connection, scene_id: int) -> dict:
    """The clip dict the meta maker renders from (same fields as meta.textcard.find_clips)."""
    r = conn.execute("""SELECT sc.id, sc.start, sc.end, sc.keyframe, a.path, a.duration, a.title, a.source,
                               a.width, a.height
                          FROM scenes sc JOIN assets a ON a.id = sc.asset_id WHERE sc.id=?""", (scene_id,)).fetchone()
    return dict(r) | {"score": None, "query": ""}


# ---------- classic Shorts: subjects first, then the hero shot ----------

CLASSIC_PROMPT = """You plan YouTube Shorts for the channel "{name}" about {niche}.
Pick {n} different subjects, each one specific, well-known thing a 30-second Short can be about,
that viewers of this niche love, and that archive film or photos surely exist of (a named plane,
animal, place, machine, event; not an abstract idea).{terms}

Never repeat these earlier topics: {avoid}

Return JSON only: {{"subjects": [{{"name": "max 5 words", "show": "what a good shot of it looks like, max 10 words"}}]}}"""

HERO_SECONDS = 10.0


def classic_heroes(conn: sqlite3.Connection, settings: Settings, channel: Channel, n: int,
                   collector: Collector | None = None) -> list[int]:
    """Up to n hero scene ids for classic Shorts, topic first: Claude picks subjects, the collector
    finds a checked clip of each. The script is then written around that clip as before."""
    if n <= 0:
        return []
    from .script import past_topics
    col = collector or Collector(conn, settings, channel)
    terms = f"\nThe channel's search terms, for the kind of subjects it covers: {', '.join(channel.search_terms[:12])}." \
        if channel.search_terms else ""
    data = col._ask(CLASSIC_PROMPT.format(name=channel.name, niche=channel.niche, n=n + 2, terms=terms,
                                          avoid="; ".join(past_topics(conn, channel.id)[-40:]) or "none yet"))
    out = []
    for s in (data or {}).get("subjects", []):
        if len(out) >= n or llm.limit_reached():
            break
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        sid = col.find(name, s.get("show"), need=HERO_SECONDS)
        if sid is not None and sid not in out:
            out.append(sid)
    return out
