"""Voice-over: a free local AI voice (Kokoro-82M, Apache-2.0) reads the text blocks.

Voice is a channel setting (`voice` in its TOML: a Kokoro voice id, or empty = text and music only).
Roeland's test: half the channels with a voice, half without; the Stats page compares them. The
captions stay whole blocks: with a voice, each block is
shown for as long as it takes to say it. Whole sentences are spoken in one go (a sentence often
runs over several blocks): reading block by block gave every block its own "end of sentence" tone
and a pause in the middle. Kokoro's word timings say where each block's words lie in the audio.

assign() records the channel's voice on a new Short. prepare() speaks every block into
<render folder>/voice.wav right before footage is picked (the footage search needs each block's
length), and again when a text block is edited. A generic narrator over real footage needs no
'altered or synthetic' label; we never imitate a real person's voice."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import wave
from pathlib import Path

import numpy as np

from .config import Channel, Settings

log = logging.getLogger(__name__)

SR = 24000          # Kokoro's sample rate
GAP = 0.25          # seconds of silence after each spoken sentence
MIN_BLOCK = 0.8     # a block mid-sentence is on screen at least this long (else a short pause is added)
VERSION = 2         # voice.json from before whole-sentence speech gets spoken again
# English voices (a = American, b = British; f/m = female/male). Kokoro has more; these sound best.
VOICES = ["am_michael", "am_fenrir", "am_puck", "af_heart", "af_bella", "af_nicole",
          "bm_george", "bm_fable", "bf_emma", "bf_isabella"]
SPEEDS = [0.9, 0.95, 1.0, 1.05, 1.1, 1.15]
SAMPLE_TEXT = ("In 1969, two astronauts walked on the Moon. The footprints they left behind "
               "are still there today, because the Moon has no wind to blow them away.")


def label(v: str) -> str:
    """'am_michael' -> 'Michael, American man'."""
    accent = {"a": "American", "b": "British"}.get(v[:1], "")
    who = {"m": "man", "f": "woman"}.get(v[1:2], "")
    return f"{v.split('_', 1)[-1].capitalize()}, {accent} {who}".strip()


class Kokoro:
    def __init__(self):
        from kokoro import KPipeline
        self._KPipeline = KPipeline
        self._pipes: dict[str, object] = {}

    def _pipe(self, voice: str):
        lang = voice[0] if voice[:1] in ("a", "b") else "a"
        if lang not in self._pipes:
            self._pipes[lang] = self._KPipeline(lang_code=lang)
        return self._pipes[lang]

    def speak(self, text: str, voice: str, speed: float = 1.0) -> np.ndarray:
        return self.speak_words(text, voice, speed)[0]

    def speak_words(self, text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, list | None]:
        """(audio, [(word, start s, end s), ...]); the words are None when Kokoro gave no timings."""
        parts, words, offset, timed = [], [], 0.0, True
        for r in self._pipe(voice)(text, voice=voice, speed=speed):
            audio = getattr(r, "audio", None)
            if audio is None:
                continue
            audio = np.asarray(audio, dtype=np.float32)
            for t in getattr(r, "tokens", None) or []:
                if not any(c.isalnum() for c in (t.text or "")):
                    continue  # punctuation
                if t.start_ts is None or t.end_ts is None:
                    timed = False
                    continue
                words.append((t.text, offset + float(t.start_ts), offset + float(t.end_ts)))
            if not getattr(r, "tokens", None):
                timed = False
            parts.append(audio)
            offset += len(audio) / SR
        audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return audio, (words if timed and words else None)


_engine = None  # None = not loaded yet; False = not available
_lock = threading.Lock()  # one model, several render threads


def set_engine(engine) -> None:
    """Tests put a fake engine here; False = no voice available."""
    global _engine
    _engine = engine


_load_lock = threading.Lock()  # make loads the model in a warm-up thread: once, not twice


def engine():
    global _engine
    if _engine is None:
        with _load_lock:
            if _engine is None:
                try:
                    _engine = Kokoro()
                except Exception as e:  # noqa: BLE001 - not installed: Shorts stay text-only
                    log.warning("voice-over not available (%s): run 'python -m pip install kokoro'", e)
                    _engine = False
    return _engine


def warm_up(voices: set[str]) -> None:
    """Load the model and these voices before the first Short needs them (make does this in a
    thread while Claude writes the first scripts)."""
    eng = engine()
    if not eng:
        return
    for v in voices:
        with _lock:
            eng.speak("Hi.", v)


def available() -> bool:
    """Cheap check for the dashboard: doesn't load the model."""
    if _engine is None:
        import importlib.util
        return importlib.util.find_spec("kokoro") is not None
    return bool(_engine)


def channel_voice(channel: Channel) -> str | None:
    """The channel's voice, or None for a text-and-music channel."""
    v = (channel.voice or "").strip()
    return None if v.lower() in ("", "off", "none") else v


def assign(conn: sqlite3.Connection, channel: Channel, short_id: int) -> str | None:
    """A new Short gets its channel's voice (kept on the Short: changing the channel later doesn't
    change Shorts already made)."""
    chosen = channel_voice(channel)
    conn.execute("UPDATE shorts SET voice=? WHERE id=?", (chosen, short_id))
    conn.commit()
    return chosen


def _speakable(text: str) -> str:
    return text.replace("*", "").strip()


def _key(blocks: list[dict], voice: str, speed: float) -> str:
    return hashlib.sha1(json.dumps([[b["text"] for b in blocks], voice, speed, VERSION]).encode()).hexdigest()


def sentences(blocks: list[dict]) -> list[list[int]]:
    """Block indexes grouped into sentences: a sentence ends on a block ending in . ! ? or …"""
    groups, cur = [], []
    for i, b in enumerate(blocks):
        cur.append(i)
        if re.search(r"[.!?…][\"')\]]*$", _speakable(b["text"])):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def speak_words(eng, text: str, voice: str, speed: float) -> tuple[np.ndarray, list | None]:
    if hasattr(eng, "speak_words"):
        return eng.speak_words(text, voice, speed)
    return eng.speak(text, voice, speed), None


def block_cuts(texts: list[str], audio_len: float, words: list | None) -> list[float]:
    """Where in a sentence's audio (seconds) each block after the first starts: halfway between
    the last word of one block and the first word of the next. Without usable word timings
    (or when Kokoro split the words differently), by share of letters."""
    counts = [len(t.split()) for t in texts]
    if words and len(words) == sum(counts):
        cuts, n = [], 0
        for c in counts[:-1]:
            n += c
            cuts.append((words[n - 1][2] + words[n][1]) / 2)
        return cuts
    letters = [max(len(t.replace(" ", "")), 1) for t in texts]
    total, cuts, run = sum(letters), [], 0
    for c in letters[:-1]:
        run += c
        cuts.append(audio_len * run / total)
    return cuts


def write_wav(path: Path, samples: np.ndarray, rate: int = SR) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def prepare(conn: sqlite3.Connection, settings: Settings, channel: Channel, short_id: int) -> Path | None:
    """Speak the blocks of a voiced Short into voice.wav and stretch each block to its speech.
    Returns the wav, or None for a text-only Short. Does nothing when the text didn't change."""
    from .render import work_dir
    from .script import END_LINGER
    s = conn.execute("SELECT voice FROM shorts WHERE id=?", (short_id,)).fetchone()
    if not s or not s["voice"]:
        return None
    blocks = [dict(r) for r in conn.execute("SELECT * FROM blocks WHERE short_id=? ORDER BY idx", (short_id,))]
    work = work_dir(settings, channel, short_id)
    wav, meta = work / "voice.wav", work / "voice.json"
    key = _key(blocks, s["voice"], channel.voice_speed)
    if wav.exists() and meta.exists() and json.loads(meta.read_text(encoding="utf-8")).get("key") == key:
        return wav
    eng = engine()
    if not eng:
        conn.execute("UPDATE shorts SET voice=NULL WHERE id=?", (short_id,))
        conn.commit()
        return None
    groups = sentences(blocks)
    texts = [_speakable(b["text"]) for b in blocks]
    with _lock:
        try:
            spoken = [speak_words(eng, " ".join(texts[i] for i in g), s["voice"], channel.voice_speed) for g in groups]
        except Exception:  # noqa: BLE001 - a voice problem must not lose the Short: text only
            log.exception("voice-over failed for short %d; it stays text-only", short_id)
            conn.execute("UPDATE shorts SET voice=NULL WHERE id=?", (short_id,))
            conn.commit()
            return None
    parts: list[np.ndarray] = [np.zeros(0, dtype=np.float32)] * len(blocks)
    # Per block: when each of its words is said, seconds from the start of the block (for the
    # spoken-word highlight in the captions); None without usable word timings.
    timings: list[list | None] = [None] * len(blocks)
    for g, (audio, words) in zip(groups, spoken):
        # Cut the sentence at the block boundaries: the audio itself runs on without a gap.
        cuts = [int(round(c * SR)) for c in block_cuts([texts[i] for i in g], len(audio) / SR, words)]
        edges = [0, *cuts, len(audio)]
        matched = words if words and len(words) == sum(len(texts[i].split()) for i in g) else None
        n = 0
        for k, i in enumerate(g):
            count = len(texts[i].split())
            if matched:
                off = edges[k] / SR
                timings[i] = [[round(max(w[1] - off, 0), 3), round(w[2] - off, 3)] for w in matched[n:n + count]]
            n += count
            piece = audio[edges[k]:edges[k + 1]]
            tail = GAP if k == len(g) - 1 else max(0.0, MIN_BLOCK - len(piece) / SR)
            if i == len(blocks) - 1:
                tail += END_LINGER
            parts[i] = np.concatenate([piece, np.zeros(int(round(tail * SR)), dtype=np.float32)])
    total = sum(len(p) for p in parts) / SR
    if total < channel.min_seconds:  # short script: spread the extra time over the sentence ends
        ends = [g[-1] for g in groups]
        extra = int(round((channel.min_seconds - total) / len(ends) * SR))
        for i in ends:
            parts[i] = np.concatenate([parts[i], np.zeros(extra, dtype=np.float32)])
    seconds = [round(len(p) / SR, 3) for p in parts]
    words = sum(len(t.split()) for t in texts)
    spoken_s = sum(len(a) for a, _ in spoken) / SR
    if spoken_s:
        # Research: about 150-170 words a minute works for Shorts; voice_speed changes it.
        log.info("short %d: voice speaks %.0f words a minute in %d sentence(s)", short_id, 60 * words / spoken_s, len(groups))
    work.mkdir(parents=True, exist_ok=True)
    write_wav(wav, np.concatenate(parts))
    meta.write_text(json.dumps({"key": key, "seconds": seconds, "words": timings}), encoding="utf-8")
    conn.executemany("UPDATE blocks SET seconds=? WHERE id=?", [(x, b["id"]) for x, b in zip(seconds, blocks)])
    conn.execute("UPDATE shorts SET duration=? WHERE id=?", (sum(seconds), short_id))
    conn.commit()
    return wav


def word_timings(work: Path) -> list | None:
    """voice.json's per-block word timings, or None (text-only Short, older voice.json)."""
    try:
        return json.loads((work / "voice.json").read_text(encoding="utf-8")).get("words")
    except (OSError, ValueError):
        return None


def sample(settings: Settings, v: str, speed: float = 1.0, text: str | None = None) -> Path:
    """One wav for the Voices page play buttons, made once and kept in <data_dir>/voice-test."""
    if v not in VOICES:
        raise ValueError(f"unknown voice {v}")
    text = _speakable(text or SAMPLE_TEXT)
    out = settings.data_dir / "voice-test"
    h = hashlib.sha1(text.encode()).hexdigest()[:8]
    path = out / f"sample-{v}-{speed:g}-{h}.wav"
    if path.exists():
        return path
    eng = engine()
    if not eng:
        raise RuntimeError("Kokoro is not installed: python -m pip install kokoro")
    out.mkdir(parents=True, exist_ok=True)
    with _lock:
        audio = eng.speak(text, v, speed)
    write_wav(path, audio)
    return path
