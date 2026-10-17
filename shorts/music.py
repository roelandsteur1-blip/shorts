"""Background music: fetched by itself, sorted by mood, one track per Short.

Every channel has a folder <data_dir>/music/<music_folder or channel id>. Two ways in:
- fetch(): downloads CC0 / public-domain music from Openverse (no key) and Freesound (free key),
  searching the channel's music_terms. Anything with another licence is never downloaded.
- files put in the folder by hand (YouTube Audio Library, "Attribution not required").

Each track gets a row in `tracks`: licence and source, its mood and how much it sounds like
instrumental music (CLAP, an audio-text model; the same idea as CLIP for scenes), and where the music
really starts (quiet intros are skipped). pick() takes the least-used track in the mood Claude gave
the script, never the one the previous Short of the channel used. A track YouTube claims is marked
'claimed' on the Music page: it moves to music/_claimed and is never picked again."""
from __future__ import annotations

import logging
import random
import re
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests

from . import media, progress, sources
from .config import Channel, Settings

log = logging.getLogger(__name__)

MUSIC_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac")
CLAIMED = "_claimed"
MAX_TRACK_SECONDS = 15 * 60
SR = 48000                  # CLAP's sample rate

# Mood -> what CLAP compares the track with. Claude picks one of these keys per script.
MOODS = {
    "calm": "calm peaceful ambient instrumental music",
    "epic": "epic powerful cinematic orchestral music",
    "mysterious": "mysterious dark atmospheric music",
    "upbeat": "upbeat energetic electronic music",
    "tense": "tense suspenseful dramatic music",
}
# Fetched tracks must sound most like the first label; the rest is what free sound sites also hold.
KIND = ["instrumental background music", "a song with singing", "people talking", "noise",
        "a sound effect", "silence"]
# Without CLAP (transformers not installed): mood from the title and tags.
MOOD_WORDS = {
    "calm": ("calm", "relax", "peace", "ambient", "soft", "piano", "chill", "gentle", "meditat"),
    "epic": ("epic", "cinematic", "orchestra", "heroic", "trailer", "power", "triumph"),
    "mysterious": ("myster", "dark", "space", "drone", "eerie", "atmospher", "cosmic"),
    "upbeat": ("upbeat", "happy", "energetic", "dance", "electro", "funk", "groove"),
    "tense": ("tense", "suspense", "horror", "thriller", "action", "drama"),
}
DEFAULT_TERMS = ["ambient", "cinematic", "calm piano", "epic orchestral"]
OPENVERSE = "https://api.openverse.org/v1/audio/"
FREESOUND = "https://freesound.org/apiv2/search/text/"
UA = {"User-Agent": "shorts-pipeline/0.2 (https://github.com/roelandsteur1-blip/shorts; "
                    "personal project; CC0 background music for YouTube Shorts)"}


def folder_name(channel: Channel | None) -> str:
    return (channel.music_folder or channel.id) if channel else ""


def folder(settings: Settings, channel: Channel | None) -> Path:
    return settings.music_dir / folder_name(channel)


# ---------- tagging: CLAP, or words when transformers isn't installed ----------

class Clap:
    """LAION CLAP via transformers, on the GPU when there is one."""
    def __init__(self, name: str = "laion/clap-htsat-unfused"):
        import torch
        from transformers import ClapModel, ClapProcessor
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = ClapModel.from_pretrained(name).to(self.device).eval()
        self.proc = ClapProcessor.from_pretrained(name)

    def _unit(self, v) -> np.ndarray:
        if not isinstance(v, self.torch.Tensor):  # newer transformers return an output object
            v = getattr(v, "pooler_output", None) if getattr(v, "pooler_output", None) is not None else v[0]
        return (v / v.norm(dim=-1, keepdim=True)).cpu().numpy()

    def audio(self, waves: list[np.ndarray]) -> np.ndarray:
        feats = self.proc.feature_extractor(waves, sampling_rate=SR, return_tensors="pt")
        with self.torch.no_grad():
            return self._unit(self.model.get_audio_features(**{k: v.to(self.device) for k, v in feats.items()}))

    def texts(self, texts: list[str]) -> np.ndarray:
        tok = self.proc.tokenizer(texts, padding=True, return_tensors="pt")
        with self.torch.no_grad():
            return self._unit(self.model.get_text_features(**{k: v.to(self.device) for k, v in tok.items()}))


_tagger = None  # None = not loaded yet; False = not available


def set_tagger(tagger) -> None:
    """Tests (and anything without a GPU budget) put their own tagger here; False = no CLAP."""
    global _tagger
    _tagger = tagger


def tagger():
    global _tagger
    if _tagger is None:
        try:
            _tagger = Clap()
        except Exception as e:  # noqa: BLE001 - missing package, no download: fall back to words
            log.warning("CLAP not available (%s); music moods come from titles and tags", e)
            _tagger = False
    return _tagger


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(100 * (x - x.max()))
    return e / e.sum()


def mood_from_words(text: str) -> str | None:
    t = text.lower()
    hits = {m: sum(w in t for w in words) for m, words in MOOD_WORDS.items()}
    best = max(hits, key=hits.get)
    return best if hits[best] else None


# ---------- analysis: where the music starts, mood, is it music at all ----------

def decode(path: Path, seconds: float = 90) -> np.ndarray:
    """Mono float32 samples at 48 kHz, the first `seconds` of the track."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-t", str(seconds), "-i", str(path), "-ac", "1",
                          "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def music_start(wave: np.ndarray) -> float:
    """First moment the track is near its usual loudness (skips fade-ins and quiet intros)."""
    win = SR // 2
    n = len(wave) // win
    if n < 8:
        return 0.0
    rms = np.sqrt((wave[: n * win].reshape(n, win) ** 2).mean(axis=1))
    smooth = np.convolve(rms, np.ones(4) / 4, mode="valid")  # 2 s
    target = 0.5 * np.percentile(rms, 90)
    if target <= 0:
        return 0.0
    first = int(np.argmax(smooth >= target)) if (smooth >= target).any() else 0
    return round(min(first * 0.5, 30.0, max(len(wave) / SR - 20, 0)), 1)


def analyse(path: Path, words: str = "") -> dict:
    """{'start', 'mood', 'music_score', 'kind'} for one file."""
    wave = decode(path)
    start = music_start(wave)
    t = tagger()
    if not t or len(wave) < SR:
        return {"start": start, "mood": mood_from_words(words + " " + path.stem), "music_score": None, "kind": None}
    s = int(start * SR)
    clip = wave[s: s + 10 * SR]
    if len(clip) < 5 * SR:
        clip = wave[: 10 * SR]
    moods = list(MOODS)
    try:
        a = t.audio([clip])[0]
        mood_p = _softmax(t.texts([MOODS[m] for m in moods]) @ a)
        kind_p = _softmax(t.texts(KIND) @ a)
    except Exception as e:  # noqa: BLE001 - a CLAP problem must not stop the batch: use words
        log.warning("CLAP failed (%s); music moods come from titles and tags", e)
        set_tagger(False)
        return analyse(path, words)
    return {"start": start, "mood": moods[int(mood_p.argmax())], "music_score": round(float(kind_p[0]), 3),
            "kind": KIND[int(kind_p.argmax())]}


def tag_pending(conn: sqlite3.Connection, settings: Settings) -> int:
    """Analyse tracks that have no mood yet (new files in the folders, earlier fetches)."""
    rows = conn.execute("SELECT * FROM tracks WHERE status='ok' AND analysed=0").fetchall()
    for i, r in enumerate(rows):
        progress.report(i, len(rows), "music: tag")
        try:
            info = analyse(Path(r["path"]), f"{r['title'] or ''} {r['note'] or ''}")
        except (subprocess.CalledProcessError, OSError) as e:
            conn.execute("UPDATE tracks SET status='rejected', note=? WHERE id=?", (f"can't read: {e}"[:200], r["id"]))
            continue
        conn.execute("UPDATE tracks SET start=?, mood=?, music_score=?, analysed=1 WHERE id=?",
                     (info["start"], info["mood"], info["music_score"], r["id"]))
    conn.commit()
    return len(rows)


# ---------- the folders ----------

def sync(conn: sqlite3.Connection, settings: Settings) -> int:
    """Rows for files put in a music folder by hand; files that are gone are marked 'missing'."""
    added = 0
    root = settings.music_dir
    for p in [*root.glob("*"), *root.glob("*/*")]:
        if p.suffix.lower() not in MUSIC_EXT or not p.is_file() or CLAIMED in p.parts:
            continue
        fold = "" if p.parent == root else p.parent.name
        cur = conn.execute("""INSERT OR IGNORE INTO tracks (folder, path, source, title, license)
                              VALUES (?,?,?,?,?)""", (fold, str(p), "folder", p.stem, "added by hand"))
        added += cur.rowcount
    for r in conn.execute("SELECT id, path FROM tracks WHERE status='ok'").fetchall():
        if not Path(r["path"]).exists():
            conn.execute("UPDATE tracks SET status='missing' WHERE id=?", (r["id"],))
    conn.commit()
    return added


def usable(conn: sqlite3.Connection, fold: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tracks WHERE folder=? AND status='ok' ORDER BY id", (fold,)).fetchall()


# ---------- fetching ----------

def search_openverse(term: str, page: int, session: requests.Session) -> list[dict]:
    # No category=music: Openverse returns nothing for it with CC0/PD (Oct 2026); the CC0 results are
    # Freesound uploads without a category. Length and CLAP ("is this music?") do the filtering.
    r = session.get(OPENVERSE, params={"q": term, "license": "cc0,pdm",
                                       "page_size": 20, "page": page, "mature": "false"},
                    headers=UA, timeout=60)
    if r.status_code in (400, 404):  # past the last page
        return []
    r.raise_for_status()
    out = []
    for x in r.json().get("results") or []:
        if (x.get("license") or "").lower() not in ("cc0", "pdm") or not x.get("url"):
            continue
        out.append({"source": "openverse", "source_id": x["id"], "title": x.get("title") or "",
                    "creator": x.get("creator") or "", "url": x["url"],
                    "license": "CC0" if x["license"].lower() == "cc0" else "public-domain",
                    "source_url": x.get("foreign_landing_url") or "",
                    "seconds": (x.get("duration") or 0) / 1000 or None,
                    "words": " ".join(t.get("name", "") for t in x.get("tags") or [] if isinstance(t, dict))})
    return out


def search_freesound(term: str, page: int, session: requests.Session, key: str) -> list[dict]:
    r = session.get(FREESOUND, params={
        "query": f"{term} music", "token": key, "page": page, "page_size": 30, "sort": "rating_desc",
        "filter": f'license:"Creative Commons 0" duration:[{FETCH_MIN_SECONDS} TO {MAX_TRACK_SECONDS}]',
        "fields": "id,name,username,license,duration,previews,url,tags"}, headers=UA, timeout=60)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    out = []
    for x in r.json().get("results") or []:
        url = (x.get("previews") or {}).get("preview-hq-mp3")
        if "publicdomain/zero" not in (x.get("license") or "") or not url:
            continue
        out.append({"source": "freesound", "source_id": str(x["id"]), "title": x.get("name") or "",
                    "creator": x.get("username") or "", "url": url, "license": "CC0",
                    "source_url": x.get("url") or "", "seconds": x.get("duration"),
                    "words": " ".join(x.get("tags") or [])})
    return out


def music_sources(settings: Settings) -> list[str]:
    """Openverse always; Freesound once its free key is saved on the Music page."""
    return ["openverse"] + (["freesound"] if sources.api_key(settings, "freesound") else [])


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "track"


def _known(conn: sqlite3.Connection, source: str, source_id: str) -> bool:
    return conn.execute("SELECT 1 FROM tracks WHERE source=? AND source_id=?", (source, source_id)).fetchone() is not None


# Free sound sites hold far more sound effects than music: skip those by title before downloading.
SFX_WORDS = re.compile(r"\b(hit|hits|whoosh|swoosh|impact|riser|rumble|boom|sfx|foley|footsteps?|field recording|"
                       r"sound ?effects?|notification|alarm|beep|glitch|transition|stinger|jingle|rain|wind|birds?|"
                       r"thunder|crowd|traffic|engine|door|explosion|gunshot|voice|speech|scream)\b", re.I)
FETCH_MIN_SECONDS = 60   # a fetched clip shorter than this is almost never usable music
WORKERS = 6              # parallel downloads: fetching waits on the network, not the CPU
_clap_lock = threading.Lock()


def pre_check(hit: dict) -> str | None:
    """Why a search hit isn't worth downloading, or None."""
    if hit.get("seconds") and not FETCH_MIN_SECONDS <= hit["seconds"] <= MAX_TRACK_SECONDS:
        return f"length {hit['seconds']:.0f}s"
    if m := SFX_WORDS.search(f"{hit['title']} {hit.get('words', '')}"):
        return f"sound effect ('{m.group(0)}' in title/tags)"
    return None


def check_hit(settings: Settings, fold: str, hit: dict, session: requests.Session) -> dict:
    """Download one hit and judge it. No database access: runs in parallel.
    Returns {'status': 'ok'|'rejected', 'path', 'note', 'duration', 'start', 'mood', 'music_score'}."""
    dst_dir = settings.music_dir / fold
    dst_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(hit["url"].split("?")[0]).suffix.lower()
    dst = dst_dir / f"{hit['source']}-{_slug(hit['source_id'])}-{_slug(hit['title'])[:40]}{ext if ext in MUSIC_EXT else '.mp3'}"
    try:
        with session.get(hit["url"], headers=UA, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dst, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    if f.tell() > 60_000_000:
                        raise ValueError("file too large")
        duration = media.probe(dst)["duration"]
        if not FETCH_MIN_SECONDS <= duration <= MAX_TRACK_SECONDS:
            raise ValueError(f"length {duration:.0f}s")
        with _clap_lock:  # one model on the GPU
            info = analyse(dst, f"{hit['title']} {hit.get('words', '')}")
    except (requests.RequestException, ValueError, KeyError, subprocess.CalledProcessError, OSError) as e:
        dst.unlink(missing_ok=True)
        return {"status": "rejected", "note": str(e)[:200]}
    keep = {k: info[k] for k in ("start", "mood", "music_score")} | {"duration": duration}
    if info["kind"] and info["kind"] != KIND[0]:
        dst.unlink(missing_ok=True)
        return {"status": "rejected", "note": f"sounds like {info['kind']}"} | keep
    return {"status": "ok", "path": str(dst)} | keep


def store_hit(conn: sqlite3.Connection, fold: str, hit: dict, res: dict) -> None:
    conn.execute("""INSERT OR IGNORE INTO tracks (folder, path, source, source_id, title, creator, license,
                        source_url, duration, start, mood, music_score, status, note, analysed)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (fold, res.get("path"), hit["source"], hit["source_id"], hit["title"], hit["creator"],
                  hit["license"], hit["source_url"], res.get("duration"), res.get("start") or 0, res.get("mood"),
                  res.get("music_score"), res["status"], res.get("note"), int("start" in res)))
    conn.commit()


def fetch(conn: sqlite3.Connection, settings: Settings, channel: Channel, want: int,
          session: requests.Session | None = None, max_pages: int = 5) -> int:
    """Download tracks until the channel has `want` usable ones (or the searches run dry)."""
    s = session or requests.Session()
    fold = folder_name(channel)
    sync(conn, settings)
    need = want - len(usable(conn, fold))
    if need <= 0:
        return 0
    terms = channel.music_terms or DEFAULT_TERMS
    got = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for page in range(1, max_pages + 1):
            for term in terms:
                for src in music_sources(settings):
                    if got >= need:
                        return got
                    try:
                        hits = (search_openverse(term, page, s) if src == "openverse"
                                else search_freesound(term, page, s, sources.api_key(settings, "freesound")))
                    except (requests.RequestException, ValueError) as e:
                        log.warning("music search %s '%s' failed: %s", src, term, e)
                        continue
                    todo = []
                    for hit in hits:
                        if _known(conn, hit["source"], hit["source_id"]) or any(
                                h["source_id"] == hit["source_id"] for h in todo):
                            continue
                        if why := pre_check(hit):
                            store_hit(conn, fold, hit, {"status": "rejected", "note": why})
                        else:
                            todo.append(hit)
                    log.info("music: %s '%s' page %d: %d CC0/public-domain results, %d worth downloading",
                             src, term, page, len(hits), len(todo))
                    # Download a few more than still needed at once: most turn out not to be music.
                    while todo and got < need:
                        batch, todo = todo[:WORKERS], todo[WORKERS:]
                        for hit, res in zip(batch, pool.map(lambda h: check_hit(settings, fold, h, s), batch)):
                            if got >= need and res["status"] == "ok":
                                Path(res["path"]).unlink(missing_ok=True)
                                continue  # not stored: may be fetched again another time
                            store_hit(conn, fold, hit, res)
                            if res["status"] == "ok":
                                got += 1
                                log.info("music: kept %s (%s, %s, %s)", hit["title"], hit["source"], hit["license"], res["mood"])
                        progress.report(got, need, "music: download")
    return got


def restock(conn: sqlite3.Connection, settings: Settings, channel: Channel) -> None:
    """Before 'make': tag new hand-added tracks, and fetch when the channel runs low. Never fails
    the batch: without music a Short is silent, not broken."""
    try:
        sync(conn, settings)
        tag_pending(conn, settings)
        if settings.music_min_tracks and len(usable(conn, folder_name(channel))) < settings.music_min_tracks:
            n = fetch(conn, settings, channel, settings.music_min_tracks)
            log.info("music: fetched %d tracks for %s", n, channel.id)
    except Exception:  # noqa: BLE001
        log.exception("music restock failed; Shorts use the tracks there are")


# ---------- picking ----------

def pick(conn: sqlite3.Connection, settings: Settings, short_id: int, channel: Channel | None) -> sqlite3.Row | None:
    """The least-used track in the Short's mood (any mood when none fits), never the track the
    channel's previous Short got. Loose files in music/ are the fallback for every channel."""
    sync(conn, settings)
    tracks = [t for t in usable(conn, folder_name(channel)) if Path(t["path"]).exists()] if channel else []
    tracks = tracks or [t for t in usable(conn, "") if Path(t["path"]).exists()]
    if not tracks:
        return None
    row = conn.execute("SELECT mood, channel FROM shorts WHERE id=?", (short_id,)).fetchone()
    mood = row["mood"] if row else None
    if mood and (same := [t for t in tracks if t["mood"] == mood]):
        tracks = same
    if len(tracks) > 1 and row:
        prev = conn.execute("""SELECT music FROM shorts WHERE channel=? AND id<? AND music IS NOT NULL
                                ORDER BY id DESC LIMIT 1""", (row["channel"], short_id)).fetchone()
        if prev:
            tracks = [t for t in tracks if t["path"] != prev["music"]] or tracks
    uses = dict(conn.execute("""SELECT music, COUNT(*) FROM shorts WHERE music IS NOT NULL AND id != ?
                                 AND status != 'rejected' GROUP BY music""", (short_id,)).fetchall())
    least = min(uses.get(t["path"], 0) for t in tracks)
    return random.Random(short_id).choice([t for t in tracks if uses.get(t["path"], 0) == least])


# ---------- claims ----------

def claim(conn: sqlite3.Connection, settings: Settings, track_id: int) -> list[int]:
    """YouTube claimed this track: never use it again. Returns the Shorts that use it and are not
    uploaded yet (re-render those)."""
    t = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    if t is None:
        raise KeyError(track_id)
    new_path = t["path"]
    if t["path"] and Path(t["path"]).exists():
        dst = settings.music_dir / CLAIMED / Path(t["path"]).name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(t["path"], dst)
        new_path = str(dst)
    conn.execute("UPDATE tracks SET status='claimed', note='claimed by YouTube', path=? WHERE id=?",
                 (new_path, track_id))
    conn.commit()
    return [r[0] for r in conn.execute(
        """SELECT id FROM shorts WHERE music=? AND youtube_id IS NULL
            AND status IN ('rendered', 'approved', 'failed') ORDER BY id""", (t["path"],))]


def set_mood(conn: sqlite3.Connection, track_id: int, mood: str) -> None:
    if mood not in MOODS:
        raise ValueError(mood)
    conn.execute("UPDATE tracks SET mood=?, analysed=1 WHERE id=?", (mood, track_id))
    conn.commit()


def remove(conn: sqlite3.Connection, track_id: int) -> None:
    """Don't like it: delete the file; the row stays so it is never fetched again."""
    t = conn.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
    if t and t["path"]:
        Path(t["path"]).unlink(missing_ok=True)
    conn.execute("UPDATE tracks SET status='rejected', note='removed by hand' WHERE id=?", (track_id,))
    conn.commit()


def summary(conn: sqlite3.Connection, fold: str) -> dict:
    rows = usable(conn, fold)
    moods: dict[str, int] = {}
    for r in rows:
        moods[r["mood"] or "untagged"] = moods.get(r["mood"] or "untagged", 0) + 1
    return {"tracks": len(rows), "moods": moods}

