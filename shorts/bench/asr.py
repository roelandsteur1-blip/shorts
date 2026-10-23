"""Narration of the original films (approach T6): what the narrator says while a scene runs.

Our copies have no sound (stripped at download), so the audio is read again from the source's
file_url with ffmpeg (audio only, nothing kept on disk) and transcribed with faster-whisper on
the CPU. Each scene gets the words spoken from 1 s before it starts to 1 s after it ends."""
from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import load, root, save, snapshot, status_line

log = logging.getLogger(__name__)
RATE = 16000


def run(settings, ts: dict, model_size: str = "base", threads: int = 4, max_assets: int | None = None,
        only: set[int] | None = None) -> dict:
    """only: transcribe just these videos (the ones a method ranked high for some block)."""
    path = root(settings) / "asr.json"
    meta_path = root(settings) / "asr-meta.json"
    texts: dict[str, str] = load(path, {})
    meta: dict[str, dict] = load(meta_path, {})
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper is not installed: narration test skipped")
        return texts
    model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=threads)
    conn = snapshot(settings)
    by_asset: dict[int, list] = defaultdict(list)
    for sid, s in ts["scenes"].items():
        by_asset[s["asset_id"]].append((sid, s["start"], s["end"]))
    assets = [a for a in sorted(by_asset) if str(a) not in meta and (only is None or a in only)]
    if max_assets is not None:
        assets = assets[:max_assets]
    for n, aid in enumerate(assets):
        row = conn.execute("SELECT file_url, duration FROM assets WHERE id=?", (aid,)).fetchone()
        status_line(settings, "narration", n, len(assets), f"video {aid}")
        if not row or not row["file_url"]:
            meta[str(aid)] = {"error": "no file_url"}
            continue
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmp:
            # Raw 16 kHz mono samples, handed to Whisper as an array: a file path would make
            # faster-whisper decode it with PyAV, whose newer versions it can't call.
            raw = Path(tmp) / "a.raw"
            try:
                proc = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", row["file_url"], "-vn",
                                       "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(RATE),
                                       str(raw)], capture_output=True, text=True, timeout=1800)
                ok = proc.returncode == 0
            except subprocess.TimeoutExpired:
                ok = False
            fetch = time.perf_counter() - t0
            if not ok or not raw.exists() or raw.stat().st_size < 10_000:
                meta[str(aid)] = {"error": "no audio", "fetch_s": fetch}
                save(meta_path, meta)
                continue
            audio = np.fromfile(raw, np.int16).astype(np.float32) / 32768.0
        t1 = time.perf_counter()
        try:
            segments, info = model.transcribe(audio, vad_filter=True, beam_size=1)
            segs = [(s.start, s.end, s.text) for s in segments]
        except Exception as e:  # one bad video must not stop the run
            log.warning("narration of video %s failed: %s", aid, e)
            meta[str(aid)] = {"error": f"{type(e).__name__}: {e}"[:300], "fetch_s": fetch}
            save(meta_path, meta)
            continue
        asr_s = time.perf_counter() - t1
        del audio
        for sid, start, end in by_asset[aid]:
            words = " ".join(t for a, b, t in segs if b >= start - 1 and a <= end + 1).strip()
            if words:
                texts[sid] = words
        meta[str(aid)] = {"fetch_s": fetch, "asr_s": asr_s, "audio_s": info.duration,
                          "speech": bool(segs), "language": info.language}
        save(path, texts)
        save(meta_path, meta)
    return texts
