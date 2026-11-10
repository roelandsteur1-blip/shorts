"""Transcript research for the transcript-first paper (plan/transcript-first-paper-plan.md, step 6).

Read-only. Two questions, measured on our own server (6 vCPU, no GPU):

1. Captions: how many free long-form items already come with captions? It counts subtitle files
   (.srt / .vtt) on archive.org items of a few free collections, NASA's image library captions
   endpoint, and whether yt-dlp can list subtitles on the server (it is bot-blocked for downloads).
2. Speech to text: how fast are the faster-whisper models on the server CPU, how much memory do
   they need, and how close are the small models to the big ones (word error rate against
   large-v3-turbo, and against NASA's own captions where an item has them)?

Every model run is its own child process, so its time and peak memory are clean. Output goes to
~/meta-out/transcribe/*.json (results.json + one transcript per source).

    python -m meta.transcribe              # whole test (about 1-2 hours)
    python -m meta.transcribe --quick      # smoke test: 2 minutes of audio, tiny model only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from .clips import UA, item_video, research_venv, search

OUT = Path.home() / "meta-out" / "transcribe"
THREADS = 4             # same as the clip maker; leaves 2 cores for the dashboard
FULL_MODELS = ["tiny.en", "base.en", "small.en"]                      # on the whole source audio
SAMPLE_MODELS = ["medium.en", "distil-large-v3", "large-v3-turbo"]    # on the 10-minute sample only
REFERENCE = "large-v3-turbo"
SAMPLE_SECONDS = 600
MAX_SOURCE_SECONDS = 1800   # at most 30 minutes per source keeps the whole job under ~2 hours

# free long-form sources (rights as in research/clip-channels.md); the first search that gives a
# long enough item wins
SOURCES = {
    "classictv": {
        "label": "Classic TV episode (1950s, archive.org classic_tv)",
        "searches": ["collection:(classic_tv) AND mediatype:movies AND (year:[1949 TO 1963])",
                     "collection:(classic_tv) AND mediatype:movies"],
        "min": 900,
    },
    "educational": {
        "label": "Educational / sponsored film with narration (Prelinger)",
        "searches": ["collection:(prelinger) AND licenseurl:(*publicdomain*) AND subject:(education)",
                     "collection:(prelinger) AND licenseurl:(*publicdomain*)"],
        "min": 600,
    },
    "newsreel": {
        "label": "Universal Newsreel issue",
        "searches": ['title:("Universal Newsreel") AND mediatype:movies'],
        "min": 300,
    },
}
CAPTION_COLLECTIONS = {
    "classic_tv": "collection:(classic_tv) AND mediatype:movies",
    "prelinger": "collection:(prelinger) AND mediatype:movies",
    "universal_newsreels": 'title:("Universal Newsreel") AND mediatype:movies',
    "nasa (archive.org)": "collection:(nasa) AND mediatype:movies",
    "feature_films": "collection:(feature_films) AND mediatype:movies",
    "classic_cartoons": "collection:(classic_cartoons) AND mediatype:movies",
}
NASA_QUERIES = ["press conference", "briefing", "documentary", "launch coverage", "interview"]
SUB_EXT = (".srt", ".vtt", ".sbv", ".ass", ".ssa")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- part 1: captions that already exist ----------

def archive_captions(rows: int = 40) -> dict:
    import requests
    out = {}
    for name, q in CAPTION_COLLECTIONS.items():
        try:
            docs = search(q, rows=rows)
        except Exception as e:  # noqa: BLE001 - research job: record and go on
            out[name] = {"error": str(e)}
            continue
        have, kinds = 0, {}
        for d in docs:
            try:
                r = requests.get(f"https://archive.org/metadata/{quote(d['identifier'])}", headers=UA, timeout=60)
                files = r.json().get("files", [])
            except Exception:  # noqa: BLE001
                continue
            subs = [f["name"] for f in files if str(f.get("name", "")).lower().endswith(SUB_EXT)]
            if subs:
                have += 1
                for s in subs:
                    k = "asr" if "asr" in s.lower() else s.rsplit(".", 1)[-1].lower()
                    kinds[k] = kinds.get(k, 0) + 1
        out[name] = {"checked": len(docs), "with_subtitles": have, "kinds": kinds}
        log(f"captions {name}: {have}/{len(docs)} items have subtitle files {kinds}")
    return out


def nasa_captions(per_query: int = 10) -> dict:
    import requests
    checked, have, examples = 0, 0, []
    for q in NASA_QUERIES:
        try:
            r = requests.get("https://images-api.nasa.gov/search", params={"q": q, "media_type": "video"},
                             headers=UA, timeout=60)
            items = r.json().get("collection", {}).get("items", [])[:per_query]
        except Exception as e:  # noqa: BLE001
            log(f"nasa search {q!r} failed: {e}")
            continue
        for it in items:
            nid = it.get("data", [{}])[0].get("nasa_id")
            if not nid:
                continue
            checked += 1
            try:
                c = requests.get(f"https://images-api.nasa.gov/captions/{quote(nid)}", headers=UA, timeout=60)
                loc = c.json().get("location") if c.ok else None
            except Exception:  # noqa: BLE001
                loc = None
            if loc:
                have += 1
                if len(examples) < 5:
                    examples.append({"nasa_id": nid, "captions": loc})
    log(f"captions NASA image library: {have}/{checked} videos have a captions file")
    return {"checked": checked, "with_captions": have, "examples": examples}


def ytdlp_subs() -> dict:
    """Can the server list YouTube subtitles with yt-dlp? (NASA's own channel, public domain.)"""
    research_venv()
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # a short, always-available video: tests access only
    py = Path.home() / "research-venv" / "bin" / "python"
    cmd = [str(py if py.exists() else sys.executable), "-m", "yt_dlp", "--skip-download", "--list-subs", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        text = (p.stdout + p.stderr)[-3000:]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    blocked = "Sign in to confirm" in text or "bot" in text.lower()
    log(f"yt-dlp list-subs: rc={p.returncode} blocked={blocked}")
    return {"ok": p.returncode == 0 and not blocked, "blocked": blocked, "tail": text}


def nasa_reference() -> dict | None:
    """One NASA video with its own captions, for a word error rate against human captions."""
    import requests
    for q in NASA_QUERIES:
        try:
            r = requests.get("https://images-api.nasa.gov/search", params={"q": q, "media_type": "video"},
                             headers=UA, timeout=60)
            items = r.json().get("collection", {}).get("items", [])[:15]
        except Exception:  # noqa: BLE001
            continue
        for it in items:
            nid = it.get("data", [{}])[0].get("nasa_id")
            try:
                c = requests.get(f"https://images-api.nasa.gov/captions/{quote(nid)}", headers=UA, timeout=60)
                loc = c.json().get("location") if c.ok else None
                if not loc:
                    continue
                files = requests.get(it["href"], headers=UA, timeout=60).json()
            except Exception:  # noqa: BLE001
                continue
            mp4 = [f for f in files if f.lower().endswith(".mp4")]
            if not mp4:
                continue
            vid = sorted(mp4, key=lambda f: ("orig" not in f, "medium" not in f))[0]
            vid = vid.replace("http://", "https://").replace(" ", "%20")
            try:
                srt = requests.get(loc.replace("http://", "https://").replace(" ", "%20"), headers=UA, timeout=60).text
            except Exception:  # noqa: BLE001
                continue
            if len(srt_words(srt)) < 150:
                continue
            return {"nasa_id": nid, "url": vid, "captions": srt}
    return None


# ---------- part 2: speech to text ----------

def fetch_audio(url: str, out: Path, seconds: float) -> float:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-t", str(int(seconds)), "-i", url, "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)], check=True, timeout=3600)
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                       capture_output=True, text=True, timeout=60)
    return float(p.stdout.strip() or 0)


def load_wav(path: str):
    """16 kHz mono PCM as float32. Decoded here, because the research venv's PyAV is too old for
    faster-whisper's own decoder (transcribe-1 failed on that)."""
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, np.int16).astype(np.float32) / 32768.0


def child(model: str, wav: str, words: bool) -> None:
    """Runs inside a child process: one model on one file, prints one JSON line."""
    import resource
    research_venv()
    from faster_whisper import WhisperModel
    t0 = time.time()
    m = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=THREADS)
    load = time.time() - t0
    t1 = time.time()
    segs, info = m.transcribe(load_wav(wav), language="en", vad_filter=True, word_timestamps=words)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
             **({"words": [[w.word, round(w.start, 2), round(w.end, 2), round(w.probability, 2)]
                           for w in (s.words or [])]} if words else {})} for s in segs]
    run = time.time() - t1
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print("RESULT " + json.dumps({"model": model, "load_s": round(load, 1), "run_s": round(run, 1),
                                  "peak_mb": round(rss), "segments": segs,
                                  "language_prob": round(getattr(info, "language_probability", 0) or 0, 3)}))


def run_model(model: str, wav: Path, words: bool = False) -> dict:
    cmd = [sys.executable, "-m", "meta.transcribe", "--child", model, str(wav)] + (["--words"] if words else [])
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3 * 3600)
    for line in p.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    return {"model": model, "error": (p.stderr or p.stdout)[-1500:]}


def norm_words(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    return re.findall(r"[a-z0-9']+", text)


def srt_words(srt: str) -> list[str]:
    lines = [ln for ln in srt.splitlines() if ln.strip() and "-->" not in ln and not ln.strip().isdigit()
             and not ln.startswith("WEBVTT")]
    return norm_words(re.sub(r"<[^>]+>", " ", " ".join(lines)))


def wer(ref: list[str], hyp: list[str]) -> float | None:
    if not ref:
        return None
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return round(prev[-1] / len(ref), 3)


def text_of(res: dict, until: float | None = None) -> list[str]:
    return norm_words(" ".join(s["text"] for s in res.get("segments", []) if until is None or s["start"] < until))


def pick_source(spec: dict) -> dict | None:
    for q in spec["searches"]:
        try:
            docs = search(q, rows=20)
        except Exception as e:  # noqa: BLE001
            log(f"search failed: {e}")
            continue
        for d in docs:
            try:
                got = item_video(d["identifier"])
            except Exception:  # noqa: BLE001
                continue
            if got and got[1] >= spec["min"]:
                return {"identifier": d["identifier"], "title": d.get("title"), "url": got[0],
                        "length": round(got[1])}
    return None


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--child", nargs=2)
    ap.add_argument("--words", action="store_true")
    ap.add_argument("--no-captions", action="store_true", help="skip part 1 (already measured)")
    a = ap.parse_args(argv)
    if a.child:
        child(a.child[0], a.child[1], a.words)
        return
    OUT.mkdir(parents=True, exist_ok=True)
    full_models = ["tiny.en"] if a.quick else FULL_MODELS
    sample_models = [] if a.quick else SAMPLE_MODELS
    cap_seconds = 120 if a.quick else MAX_SOURCE_SECONDS
    results = {"threads": THREADS, "cpus": os.cpu_count(), "started": time.strftime("%Y-%m-%d %H:%M:%S"),
               "captions": {}, "sources": {}}
    save = lambda: (OUT / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")  # noqa: E731

    if a.no_captions:
        results["captions"] = {"skipped": "measured in transcribe-1"}
        results["captions"]["yt_dlp"] = ytdlp_subs()
    else:
        log("part 1: which free sources already have captions")
        part1(results, a.quick)
    save()
    part2(results, a, full_models, sample_models, cap_seconds, save)


def part1(results: dict, quick: bool) -> None:
    a = argparse.Namespace(quick=quick)
    results["captions"]["archive_org"] = archive_captions(10 if a.quick else 40)
    results["captions"]["nasa_image_library"] = nasa_captions(3 if a.quick else 10)
    results["captions"]["yt_dlp"] = ytdlp_subs()


def part2(results: dict, a, full_models, sample_models, cap_seconds, save) -> None:
    log("part 2: speech to text on the server CPU")
    sources = {k: dict(v) for k, v in SOURCES.items()}
    with tempfile.TemporaryDirectory() as tmp:
        for key, spec in list(sources.items()) + [("nasa", None)]:
            if spec is None:
                ref = nasa_reference()
                if not ref:
                    log("no NASA video with captions found")
                    continue
                src = {"identifier": ref["nasa_id"], "title": ref["nasa_id"], "url": ref["url"], "label":
                       "NASA video with NASA's own captions"}
                human = srt_words(ref["captions"])
            else:
                src = pick_source(spec)
                if not src:
                    log(f"{key}: no source found")
                    results["sources"][key] = {"error": "no source found"}
                    continue
                src["label"] = spec["label"]
                human = None
            log(f"{key}: {src['title']} ({src['url']})")
            wav = Path(tmp) / f"{key}.wav"
            t0 = time.time()
            try:
                secs = fetch_audio(src["url"], wav, cap_seconds)
            except Exception as e:  # noqa: BLE001
                results["sources"][key] = {**src, "error": f"audio fetch failed: {e}"}
                save()
                continue
            src["audio_seconds"] = round(secs)
            src["fetch_s"] = round(time.time() - t0, 1)
            sample = Path(tmp) / f"{key}-sample.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), "-t",
                            str(SAMPLE_SECONDS), "-c", "copy", str(sample)], check=True, timeout=300)
            sample_secs = min(secs, SAMPLE_SECONDS)
            runs = {}
            for m in full_models:
                log(f"{key}: {m} on {secs / 60:.1f} min")
                r = run_model(m, wav)
                if "run_s" in r:
                    r["x_realtime"] = round(secs / max(r["run_s"], 0.1), 1)
                    log(f"{key}: {m} {r['run_s']} s ({r['x_realtime']}x real time, {r['peak_mb']} MB)")
                runs[m] = r
            for m in sample_models:
                log(f"{key}: {m} on the {sample_secs / 60:.0f}-min sample")
                r = run_model(m, sample)
                if "run_s" in r:
                    r["x_realtime"] = round(sample_secs / max(r["run_s"], 0.1), 1)
                    log(f"{key}: {m} {r['run_s']} s ({r['x_realtime']}x real time, {r['peak_mb']} MB)")
                runs[m] = r
            if not a.quick:
                log(f"{key}: small.en with word timestamps on the sample")
                r = run_model("small.en", sample, words=True)
                if "run_s" in r:
                    r["x_realtime"] = round(sample_secs / max(r["run_s"], 0.1), 1)
                runs["small.en+words"] = r
            ref_words = text_of(runs.get(REFERENCE, {}))
            for m, r in runs.items():
                if "segments" not in r:
                    continue
                hyp = text_of(r, until=SAMPLE_SECONDS)
                r["words_in_sample"] = len(hyp)
                if ref_words and m != REFERENCE:
                    r["wer_vs_turbo"] = wer(ref_words, hyp)
                if human and (m not in sample_models or secs <= SAMPLE_SECONDS):
                    r["wer_vs_human_captions"] = wer(human, text_of(r))
            # keep full transcripts on disk, but only the small model's segments in results.json
            for m, r in runs.items():
                if "segments" in r:
                    (OUT / f"transcript-{key}-{m.replace('/', '_')}.json").write_text(
                        json.dumps({**src, "model": m, "segments": r["segments"]}, indent=0), encoding="utf-8")
                    r["segments"] = len(r["segments"])
            if human:
                src["human_caption_words"] = len(human)
            results["sources"][key] = {**src, "runs": runs}
            save()
    results["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save()
    log("done: ~/meta-out/transcribe/results.json")


if __name__ == "__main__":
    main()
