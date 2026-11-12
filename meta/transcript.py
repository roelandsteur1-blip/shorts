"""Transcripts for the transcript-first maker (plan/transcript-first-build-plan.md, phase 1).

One call, `get(source)`, gives a cached transcript of a long free video as numbered sentences
with a time for every word, so the moment finder can pick sentences and the cutter can cut on
word borders. Where the text comes from, best first (paper section 6):

1. NASA's own captions (images-api.nasa.gov/captions/{nasa_id}): human-made, used as is.
2. An archive.org subtitle file that is not archive.org's own speech recognition ("asr").
3. faster-whisper distil-large-v3 on the CPU: int8, 4 threads, voice-activity filter, word times,
   in a child process at nice 19 next to the dashboard (about 20 minutes per 30-minute source).

Captions have line times but no word times: words are spread over their line by length, which is
close enough to cut on a line border. Whisper output is checked for hallucinations (a line repeated
in a loop, "thanks for watching" over silence).

Cache: ~/meta-out/transcripts/<source id>.json. A source is transcribed once.

    python -m meta.transcript --child <wav>     # used internally: one Whisper run, prints JSON
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CACHE = Path.home() / "meta-out" / "transcripts"
MODEL = "distil-large-v3"   # paper 6.4: as fast as small.en, with sentences; turbo hallucinated
THREADS = 4                 # leaves 2 cores for the dashboard
MAX_SECONDS = 3600          # never more than an hour of audio per source
UA = {"User-Agent": "shorts-pipeline/1.0 (research samples)"}

# Whisper's typical inventions over silence and music (Koenecke et al. 2024; paper 6.2)
FILLERS = {"thank you", "thanks for watching", "thank you for watching", "thanks for watching!",
           "please subscribe", "subtitles by the amara.org community", "you", "bye", "music"}
QUIET_DB = -42.0            # below this RMS a "line" is almost certainly not speech
MAX_REPEAT = 2              # the same line more than twice in a row is a loop


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- subtitle files ----------

_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _secs(m: re.Match) -> float:
    h, mi, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_subs(text: str) -> list[dict]:
    """SRT or WebVTT -> [{start, end, text}] in order. Tags, cue settings, NOTE and STYLE
    blocks are dropped; empty cues are skipped."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        left, _, right = lines[idx].partition("-->")
        a, b = _TS.search(left), _TS.search(right)
        if not a or not b:
            continue
        words = " ".join(lines[idx + 1:])
        words = re.sub(r"<[^>]+>|\{[^}]*\}", " ", words)
        words = re.sub(r"\s+", " ", words.replace("&amp;", "&").replace("&nbsp;", " ")).strip()
        words = re.sub(r"^-\s*", "", words)
        if words:
            out.append({"start": _secs(a), "end": max(_secs(b), _secs(a) + 0.1), "text": words})
    return out


def spread_words(seg: dict) -> list[list]:
    """[[word, start, end, 1.0], ...] spread over the line by word length (captions have no word times)."""
    words = seg["text"].split()
    if not words:
        return []
    total = sum(len(w) + 1 for w in words)
    span = seg["end"] - seg["start"]
    out, t = [], seg["start"]
    for w in words:
        d = span * (len(w) + 1) / total
        out.append([w, round(t, 2), round(t + d, 2), 1.0])
        t += d
    return out


# ---------- hallucination checks ----------

def norm_line(text: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", "", text.lower().replace("’", "'")).strip()


def clean(segments: list[dict], loudness=None) -> tuple[list[dict], dict]:
    """Drop Whisper loops and fillers over silence. `loudness(start, end)` gives the RMS in dBFS
    of the audio under a line (None = unknown). Returns (kept segments, flags)."""
    kept, dropped, run = [], [], 0
    prev = None
    for s in segments:
        line = norm_line(s["text"])
        if not line:
            continue
        run = run + 1 if line == prev else 1
        prev = line
        if run > MAX_REPEAT:
            dropped.append({"t": s["start"], "text": s["text"], "why": "repeat"})
            continue
        if line in FILLERS or line.rstrip("!.") in FILLERS:
            quiet = loudness(s["start"], s["end"]) if loudness else None
            if quiet is None or quiet < QUIET_DB:
                dropped.append({"t": s["start"], "text": s["text"], "why": "filler over silence"})
                continue
        kept.append(s)
    repeats = sum(1 for d in dropped if d["why"] == "repeat")
    return kept, {"dropped": dropped, "repeats": repeats, "kept": len(kept)}


# ---------- sentences ----------

END = re.compile(r"[.!?…][\"')\]]*$")


def sentences(segments: list[dict], max_seconds: float = 20.0, max_words: int = 60) -> list[dict]:
    """Numbered sentences from segments: [{i, start, end, text, words}]. A sentence ends on . ! ?
    or, when the text has no punctuation (old captions), at a line end once it is long enough, so
    a moment can always start and end on a border."""
    out: list[dict] = []
    cur: list[list] = []

    def flush():
        if cur:
            out.append({"i": len(out), "start": cur[0][1], "end": cur[-1][2],
                        "text": " ".join(w[0] for w in cur).strip(), "words": list(cur)})
            cur.clear()

    for s in segments:
        words = s.get("words") or spread_words(s)
        for w in words:
            w = [str(w[0]).strip(), float(w[1]), float(w[2]), float(w[3]) if len(w) > 3 else 1.0]
            if not w[0]:
                continue
            if cur and w[1] - cur[-1][2] > 2.5:  # a long pause is a border too
                flush()
            cur.append(w)
            if END.search(w[0]) and not re.fullmatch(r"(Mr|Mrs|Dr|St|Jr|Sr|Lt|Col|Gen|U\.S)\.", w[0]):
                flush()
            elif len(cur) >= max_words:
                flush()
        if cur and (cur[-1][2] - cur[0][1] >= max_seconds or len(cur) >= max_words):
            flush()
    flush()
    return out


# ---------- sources of text ----------

def nasa_captions_url(nasa_id: str) -> str | None:
    import requests
    from urllib.parse import quote
    try:
        r = requests.get(f"https://images-api.nasa.gov/captions/{quote(nasa_id)}", headers=UA, timeout=60)
        loc = r.json().get("location") if r.ok else None
    except Exception:  # noqa: BLE001 - no captions is a normal answer
        return None
    return loc.replace("http://", "https://").replace(" ", "%20") if loc else None


def fetch_text(url: str) -> str:
    import requests
    r = requests.get(url, headers=UA, timeout=120)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


def fetch_audio(url: str, out: Path, seconds: float = MAX_SECONDS) -> float:
    """16 kHz mono WAV of the first `seconds` of a video URL; returns its length."""
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-t", str(int(seconds)), "-i", url, "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)], check=True, timeout=3600)
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                       capture_output=True, text=True, timeout=60)
    return float(p.stdout.strip() or 0)


def load_wav(path: str):
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, np.int16).astype(np.float32) / 32768.0


def loudness_of(audio, rate: int = 16000):
    """loudness(start, end) -> RMS dBFS of that stretch of a float32 array."""
    import numpy as np

    def f(start: float, end: float) -> float | None:
        a = audio[int(max(0, start) * rate):int(max(start + 0.05, end) * rate)]
        if not len(a):
            return None
        rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
        return 20 * np.log10(max(rms, 1e-9))
    return f


def child(wav: str) -> None:
    """Inside the child process: distil-large-v3 on one WAV, one JSON line on stdout."""
    try:
        os.nice(19)
    except OSError:
        pass
    from .clips import research_venv
    research_venv()
    from faster_whisper import WhisperModel
    t0 = time.time()
    m = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS)
    segs, info = m.transcribe(load_wav(wav), language="en", vad_filter=True, word_timestamps=True,
                              condition_on_previous_text=False)  # fewer loops on old soundtracks
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
             "words": [[w.word.strip(), round(w.start, 2), round(w.end, 2), round(w.probability, 2)]
                       for w in (s.words or [])]} for s in segs]
    print("RESULT " + json.dumps({"run_s": round(time.time() - t0, 1), "segments": segs,
                                  "language_prob": round(getattr(info, "language_probability", 0) or 0, 3)}))


def whisper(wav: Path) -> dict:
    p = subprocess.run([sys.executable, "-m", "meta.transcript", "--child", str(wav)], capture_output=True,
                       text=True, timeout=3 * 3600)
    for line in p.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    raise RuntimeError("whisper failed: " + (p.stderr or p.stdout)[-1500:])


# ---------- the one entry point ----------

def cache_path(source_id: str) -> Path:
    return CACHE / (re.sub(r"[^A-Za-z0-9_.-]+", "_", source_id)[:120] + ".json")


def get(source: dict, refresh: bool = False) -> dict:
    """Transcript of a source {id, url, captions_url?}. Cached; see the module doc for the order."""
    path = cache_path(source["id"])
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    res: dict = {"id": source["id"], "flags": {}}
    segs = []
    if source.get("captions_url"):
        try:
            segs = parse_subs(fetch_text(source["captions_url"]))
            res.update(origin=source.get("captions_origin", "captions"), model=None)
        except Exception as e:  # noqa: BLE001 - fall back to Whisper
            log(f"{source['id']}: captions unreadable ({e}), using Whisper")
            segs = []
        if segs and sum(len(s["text"].split()) for s in segs) < 50:
            log(f"{source['id']}: captions too short ({len(segs)} lines), using Whisper")
            segs = []
    if not segs:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "a.wav"
            secs = fetch_audio(source["url"], wav)
            log(f"{source['id']}: Whisper {MODEL} on {secs / 60:.1f} min of audio")
            out = whisper(wav)
            segs, flags = clean(out["segments"], loudness_of(load_wav(str(wav))))
            res.update(origin="whisper", model=MODEL, audio_s=round(secs), run_s=out["run_s"],
                       x_realtime=round(secs / max(out["run_s"], 0.1), 2), flags=flags)
    res["sentences"] = sentences(segs)
    res["words"] = sum(len(s["words"]) for s in res["sentences"])
    res["took_s"] = round(time.time() - t0, 1)
    path.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    log(f"{source['id']}: {res['origin']}, {len(res['sentences'])} sentences, {res['words']} words "
        f"in {res['took_s']} s")
    return res


def numbered(sentences_: list[dict]) -> str:
    """The transcript as Claude reads it: one sentence per line, '[12] 3:04 text'."""
    return "\n".join(f"[{s['i']}] {int(s['start'] // 60)}:{int(s['start'] % 60):02d} {s['text']}"
                     for s in sentences_)


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--child")
    a = ap.parse_args(argv)
    if a.child:
        child(a.child)


if __name__ == "__main__":
    main()
