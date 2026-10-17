"""Voice-over: a free local AI voice (Kokoro-82M, Apache-2.0) reads the text blocks.

Voice is a channel setting (`voice` in its TOML: a Kokoro voice id, or empty = text and music only).
Roeland's test: half the channels with a voice, half without; the Stats page compares them. The
captions stay whole blocks: with a voice, each block is
shown for as long as it takes to say it (never shorter than the reading-time rule).

assign() records the channel's voice on a new Short. prepare() speaks every block into
<render folder>/voice.wav right before footage is picked (the footage search needs each block's
length), and again when a text block is edited. A generic narrator over real footage needs no
'altered or synthetic' label; we never imitate a real person's voice."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import wave
from pathlib import Path

import numpy as np

from .config import Channel, Settings

log = logging.getLogger(__name__)

SR = 24000          # Kokoro's sample rate
GAP = 0.25          # seconds of silence after each spoken block
# English voices (a = American, b = British; f/m = female/male). Kokoro has more; these sound best.
VOICES = ["am_michael", "am_fenrir", "am_puck", "af_heart", "af_bella", "af_nicole",
          "bm_george", "bm_fable", "bf_emma", "bf_isabella"]


class Kokoro:
    def __init__(self):
        from kokoro import KPipeline
        self._KPipeline = KPipeline
        self._pipes: dict[str, object] = {}

    def speak(self, text: str, voice: str, speed: float = 1.0) -> np.ndarray:
        lang = voice[0] if voice[:1] in ("a", "b") else "a"
        if lang not in self._pipes:
            self._pipes[lang] = self._KPipeline(lang_code=lang)
        parts = [np.asarray(audio, dtype=np.float32) for _, _, audio in
                 self._pipes[lang](text, voice=voice, speed=speed) if audio is not None]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


_engine = None  # None = not loaded yet; False = not available
_lock = threading.Lock()  # one model, several render threads


def set_engine(engine) -> None:
    """Tests put a fake engine here; False = no voice available."""
    global _engine
    _engine = engine


def engine():
    global _engine
    if _engine is None:
        try:
            _engine = Kokoro()
        except Exception as e:  # noqa: BLE001 - not installed: Shorts stay text-only
            log.warning("voice-over not available (%s): run 'python -m pip install kokoro'", e)
            _engine = False
    return _engine


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
    return hashlib.sha1(json.dumps([[b["text"] for b in blocks], voice, speed]).encode()).hexdigest()


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
    from .script import block_seconds
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
    parts, seconds = [], []
    with _lock:
        try:
            audios = [eng.speak(_speakable(b["text"]), s["voice"], channel.voice_speed) for b in blocks]
        except Exception:  # noqa: BLE001 - a voice problem must not lose the Short: text only
            log.exception("voice-over failed for short %d; it stays text-only", short_id)
            conn.execute("UPDATE shorts SET voice=NULL WHERE id=?", (short_id,))
            conn.commit()
            return None
        for i, (b, audio) in enumerate(zip(blocks, audios)):
            spoken = len(audio) / SR
            # Never shorter than reading it; the last block lingers like the text-only version.
            secs = max(block_seconds(b["text"]), spoken + GAP) + (0.6 if i == len(blocks) - 1 else 0)
            secs = round(secs, 2)
            pad = int(round(secs * SR)) - len(audio)
            parts.append(np.concatenate([audio, np.zeros(max(pad, 0), dtype=np.float32)])[: int(round(secs * SR))])
            seconds.append(secs)
    total = sum(seconds)
    if total < channel.min_seconds:  # short script: spread the extra time over the pauses
        extra = (channel.min_seconds - total) / len(seconds)
        parts = [np.concatenate([p, np.zeros(int(round(extra * SR)), dtype=np.float32)]) for p in parts]
        seconds = [round(x + extra, 2) for x in seconds]
    work.mkdir(parents=True, exist_ok=True)
    write_wav(wav, np.concatenate(parts))
    meta.write_text(json.dumps({"key": key, "seconds": seconds}), encoding="utf-8")
    conn.executemany("UPDATE blocks SET seconds=? WHERE id=?", [(x, b["id"]) for x, b in zip(seconds, blocks)])
    conn.execute("UPDATE shorts SET duration=? WHERE id=?", (sum(seconds), short_id))
    conn.commit()
    return wav


def samples(conn: sqlite3.Connection, settings: Settings, voices: list[str], shorts: int = 3) -> Path:
    """voice-test: read the newest scripts with each voice into <data_dir>/voice-test/*.wav."""
    eng = engine()
    if not eng:
        raise RuntimeError("Kokoro is not installed: python -m pip install kokoro")
    out = settings.data_dir / "voice-test"
    out.mkdir(parents=True, exist_ok=True)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM shorts WHERE status != 'rejected' ORDER BY id DESC LIMIT ?", (shorts,))]
    for sid in ids:
        text = " ".join(_speakable(r[0]) for r in conn.execute(
            "SELECT text FROM blocks WHERE short_id=? ORDER BY idx", (sid,)))
        for v in voices:
            write_wav(out / f"short{sid}-{v}.wav", eng.speak(text, v))
            log.info("voice-test: short %d, %s", sid, v)
    return out
