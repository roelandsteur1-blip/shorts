"""Footage collector research, steps R4-R7 (+ R5b footage age): for 30 test subjects, take the best
candidates from the sources Roeland picked per niche (round 1), cut ONLY the piece we need, run
the quality gates and the blind check, and make short 9:16 preview clips for review round 2.

Read-only for the live program: nothing goes into the shorts database or footage library; files
stay in ~/meta-out/pieces/ (final previews) and a temp folder (everything else, deleted after).

    python -m meta.collector.pieces --part 1     # niches 1-5 (15 subjects)
    python -m meta.collector.pieces --part 2     # niches 6-10

Runs in ~/research-venv (yt-dlp, RapidOCR, piq; see meta/collector/tools.py) and re-starts
itself there when started from the live .venv. A missing tool only switches off its gate.

Per candidate it records: where the file came from, how it was fetched (remote cut / whole
download), seconds and bytes, the gates (text on screen, sharpness, black/frozen, colour = old or
modern, archive year) and Claude's blind verdict. One failing candidate or archive never stops
the run (R7): every step has a timeout and its error is written down.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageFilter

from . import sweep

OUT = Path.home() / "meta-out" / "pieces"
RESEARCH_PY = Path.home() / "research-venv" / "bin" / "python"
NICHES = ["space", "warplane", "ocean", "planet", "history", "retrotech", "weather", "earthquake", "oldcity", "body"]
PICK = (0, 2, 4)            # subjects per niche used (index in subjects.toml)
CLIP = 8.0                  # seconds per clip
WHOLE_BELOW = 150_000_000   # download the whole file below this size, cut remotely above
MAX_DOWNLOAD = 400_000_000
FFMPEG_TIMEOUT = 150

# Roeland's round-1 choice per niche (samples/footage/round1/review.md, 2026-11-04 23:28 UTC);
# niches he didn't judge use the warplane choice. (source, how many candidates)
DEFAULT = [("library", 2), ("commons_video", 3), ("commons_photo", 1), ("openverse", 1)]
SOURCES = {
    "space": DEFAULT + [("nasa", 2), ("pixabay", 1)],
    "retrotech": [("library", 2), ("openverse", 2)],
}

ERA_TEXTS = ("an old black and white archive film", "a modern high definition colour video")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- candidates ----------

def candidates(subject: dict, lib, scorer, keys: dict, sources: list[tuple[str, int]] | None = None) -> list[dict]:
    """The best hits per chosen source, ranked by PE-Core on their thumbnails. sources: (source,
    how many) to search; default Roeland's round-1 pick for the subject's niche."""
    out = []
    for src, n in sources if sources is not None else SOURCES.get(subject["niche"], DEFAULT):
        try:
            if src == "library":
                if not lib:
                    continue
                hits = sweep.library_hits(subject, lib)[1]
            elif src == "commons_video":
                hits = sweep.commons(f'"{subject["name"]}" filetype:video', "video")[1]
            elif src == "commons_depicts":
                if not subject.get("qid"):
                    continue
                hits = sweep.commons(f"haswbstatement:P180={subject['qid']} filetype:video", "video")[1]
            elif src == "commons_photo":
                q = subject.get("qid")
                hits = sweep.commons(f"haswbstatement:P180={q} filetype:bitmap" if q else
                                     f'"{subject["name"]}" filetype:bitmap', "photo", width=1600)[1]
                for h in hits:
                    h["file"] = h.get("thumb")  # the 1600 px rendition is enough for a 9:16 frame
            elif src == "openverse":
                hits = sweep.openverse(subject["name"])[1]
            elif src == "nasa":
                hits = sweep.nasa_video(subject["name"])[1]
            elif src == "pixabay":
                if not keys.get("pixabay"):
                    continue
                hits = sweep.pixabay(subject["name"], keys["pixabay"])[1]
            else:
                continue
        except Exception as e:
            log(f"  {src}: search failed: {str(e)[:150]}")
            out.append({"source": src, "error": f"search: {str(e)[:200]}"})
            continue
        hits = [h for h in hits if h.get("ok")][:sweep.KEEP]
        imgs = [sweep.fetch_thumb(h.get("thumb")) for h in hits]
        scores = scorer.scores(subject, [i for i in imgs if i]) if scorer and scorer.enc else []
        it = iter(scores)
        for h, im in zip(hits, imgs):
            h["thumb_score"] = next(it, None) if im else None
        hits.sort(key=lambda h: -(h.get("thumb_score") or 0))
        out += [dict(h, source=src) for h in hits[:n]]
    return out


# ---------- fetching the piece ----------

def ffmpeg(args: list[str], timeout: int = FFMPEG_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, timeout=timeout)


def duration(src: str) -> float | None:
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src],
                           capture_output=True, text=True, timeout=60)
        return float(p.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None


def frame_at(src: str, t: float, width: int = 448) -> Image.Image | None:
    try:
        p = ffmpeg(["-ss", f"{max(0.0, t):.2f}", "-i", src, "-frames:v", "1", "-vf", f"scale={width}:-2",
                    "-f", "image2", "-c:v", "mjpeg", "-"], timeout=45)
        return Image.open(io.BytesIO(p.stdout)).convert("RGB") if p.stdout else None
    except (subprocess.SubprocessError, OSError):
        return None


def download(url: str, dst: Path, limit: int = MAX_DOWNLOAD) -> int:
    with sweep.S.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        n = 0
        with open(dst, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                n += len(chunk)
                if n > limit:
                    raise RuntimeError(f"file over {limit // 1_000_000} MB")
                fh.write(chunk)
    return n


def resolve(c: dict) -> str | None:
    """The video file URL (or local path for the library) of a candidate."""
    if c["source"] == "nasa":
        from shorts.sources import nasa
        return nasa.pick_rendition(nasa.asset_files(c["nasa_id"], sweep.S))
    return c.get("file")


def best_second(src: str, secs: float, subject: dict, scorer, start: float = 0.0, end: float | None = None,
                clip: float | None = None) -> tuple[float, list]:
    """Where in [start, end] the subject shows best: a few frames scored with PE-Core."""
    clip = clip or CLIP
    end = end if end is not None else secs
    span = max(0.0, end - start - clip)
    n = 1 if span < 2 else 3 if span < 30 or (src.startswith("http") and ".webm" in src) else 8
    times = [start + span * (i + 0.5) / n + clip / 2 for i in range(n)]
    frames = [(t, frame_at(src, t)) for t in times]
    frames = [(t, f) for t, f in frames if f]
    if not frames:
        raise RuntimeError("no frame could be read")
    scores = scorer.scores(subject, [f for _, f in frames]) if scorer and scorer.enc else [0.0] * len(frames)
    best = max(range(len(frames)), key=lambda i: scores[i] or 0)
    return frames[best][0], [(round(t, 1), s) for (t, _), s in zip(frames, scores)]


def fetch_video(c: dict, subject: dict, scorer, tmp: Path, seconds: float | None = None) -> Path:
    """Cut `seconds` (default CLIP) of the candidate at its best moment into tmp/<label>.mp4 (720p,
    no audio)."""
    clip = seconds or CLIP
    rec = c.setdefault("fetch", {})
    t0 = time.time()
    url = resolve(c)
    if not url:
        raise RuntimeError("no video file")
    rec["url"] = url if c["source"] != "library" else "library file"
    src = url
    if c["source"] != "library":
        size = int(c.get("bytes") or 0)
        if not size:
            try:
                size = int(sweep.S.head(url, allow_redirects=True, timeout=30).headers.get("Content-Length") or 0)
            except requests.RequestException:
                size = 0
        rec["file_mb"] = round(size / 1e6, 1)
        if (size and size < WHOLE_BELOW) or url.lower().endswith(".webm") and size < MAX_DOWNLOAD:
            local = tmp / f"{c['label']}.src"
            rec["downloaded_mb"] = round(download(url, local) / 1e6, 1)
            rec["method"] = "whole file"
            src = str(local)
        else:
            rec["method"] = "remote cut"
    else:
        rec["method"] = "library"
    secs = duration(src) or float(c.get("seconds") or 0)
    if not secs:
        raise RuntimeError("unknown length")
    rec["video_seconds"] = round(secs)
    if c["source"] == "library":
        at = float(c["start"]) + clip / 2
        rec["sampled"] = []
    else:
        at, rec["sampled"] = best_second(src, secs, subject, scorer, clip=clip)
    start = max(0.0, min(at - clip / 2, secs - clip))
    out = tmp / f"{c['label']}.mp4"
    p = ffmpeg(["-ss", f"{start:.2f}", "-i", src, "-t", f"{clip}", "-an", "-vf", "scale=-2:720",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
    if p.returncode or not out.exists() or out.stat().st_size < 20_000:
        raise RuntimeError("cut failed: " + p.stderr.decode(errors="replace")[-200:])
    rec["at"] = round(start, 1)
    rec["seconds_taken"] = round(time.time() - t0, 1)
    return out


def fetch_photo(c: dict, tmp: Path, seconds: float = 5.0) -> Path:
    """A photo becomes a slow zoom (5 s: what quiz/ranking would show)."""
    rec = c.setdefault("fetch", {})
    t0 = time.time()
    r = sweep.S.get(c["file"], timeout=60)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    rec.update(method="photo", width=im.width, height=im.height)
    img = tmp / f"{c['label']}.jpg"
    im.save(img, quality=92)
    out = tmp / f"{c['label']}.mp4"
    p = ffmpeg(["-loop", "1", "-i", str(img), "-t", f"{seconds:.2f}", "-vf",
                "scale=1600:-2,zoompan=z='min(zoom+0.0008,1.12)':d=1:s=1280x720:fps=25",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)])
    if p.returncode or not out.exists():
        raise RuntimeError("zoom failed: " + p.stderr.decode(errors="replace")[-200:])
    rec["seconds_taken"] = round(time.time() - t0, 1)
    return out


# ---------- gates ----------

class Gates:
    """Quality gates on 3 frames of the clip. Each tool is optional (None = gate off)."""

    def __init__(self, scorer, brisque: bool = True):
        self.scorer = scorer
        self.ocr = None
        try:
            from rapidocr_onnxruntime import RapidOCR
            self.ocr = RapidOCR()
        except Exception as e:
            log(f"RapidOCR not available, text gate off: {e}")
        self.piq = None
        try:
            if not brisque:  # dropped for production: didn't predict Roeland's marks (round 2)
                raise ImportError("not used")
            import piq
            import torch
            self.piq, self.torch = piq, torch
        except Exception as e:
            log(f"piq not available, BRISQUE off: {e}")
        self.era_vec = None
        if scorer and scorer.enc:
            self.era_vec = scorer.enc.texts(list(ERA_TEXTS))

    def text_share(self, im: Image.Image) -> float | None:
        if not self.ocr:
            return None
        res, _ = self.ocr(np.array(im)[:, :, ::-1])
        area = 0.0
        for box, _text, conf in res or []:
            if float(conf) < 0.5:
                continue
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            area += (max(xs) - min(xs)) * (max(ys) - min(ys))
        return round(area / (im.width * im.height), 4)

    def brisque(self, im: Image.Image) -> float | None:
        if not self.piq:
            return None
        try:
            x = self.torch.from_numpy(np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0).permute(2, 0, 1)[None]
            return round(float(self.piq.brisque(x, data_range=1.0)), 1)
        except Exception:
            return None

    def check(self, frames: list[Image.Image]) -> dict:
        import cv2
        g = {}
        grays = [np.asarray(f.convert("L"), dtype=np.float32) for f in frames]
        g["sharpness"] = round(float(np.median([cv2.Laplacian(x, cv2.CV_32F).var() for x in grays])), 1)
        g["brightness"] = round(float(np.mean([x.mean() for x in grays])), 1)
        g["motion"] = round(float(np.mean([np.abs(a - b).mean() for a, b in zip(grays, grays[1:])])), 2) if len(grays) > 1 else None
        hsv = [np.asarray(f.convert("HSV"))[:, :, 1] for f in frames]
        g["saturation"] = round(float(np.mean([h.mean() for h in hsv])), 1)
        texts = [self.text_share(f) for f in frames]
        g["text_share"] = max(t for t in texts if t is not None) if any(t is not None for t in texts) else None
        bq = [self.brisque(f) for f in frames]
        g["brisque"] = round(float(np.median([b for b in bq if b is not None])), 1) if any(b is not None for b in bq) else None
        if self.era_vec is not None:
            with tempfile.TemporaryDirectory() as d:
                paths = []
                for i, f in enumerate(frames):
                    f.save(Path(d) / f"{i}.jpg")
                    paths.append(str(Path(d) / f"{i}.jpg"))
                iv = self.scorer.enc.images(paths)
            sims = iv @ self.era_vec.T
            g["old_look"] = round(float((sims[:, 0] - sims[:, 1]).mean()), 4)
        g["faults"] = faults(g)
        return g


def faults(g: dict) -> list[str]:
    """First-guess thresholds; R5 compares each gate with Roeland's marks before any is used."""
    out = []
    if g.get("brightness") is not None and g["brightness"] < 18:
        out.append("black")
    if g.get("motion") is not None and g["motion"] < 0.4:
        out.append("frozen")
    if g.get("text_share") is not None and g["text_share"] > 0.04:
        out.append("text on screen")
    if g.get("sharpness") is not None and g["sharpness"] < 25:
        out.append("blurry")
    return out


def clip_frames(path: Path, seconds: float) -> list[Image.Image]:
    return [f for f in (frame_at(str(path), t, 640) for t in (0.4, seconds / 2, seconds - 0.4)) if f]


def strip(frames: list[Image.Image]) -> bytes:
    h = 300
    fr = [f.resize((int(f.width * h / f.height), h)) for f in frames]
    out = Image.new("RGB", (sum(f.width for f in fr) + 20 * (len(fr) - 1), h), "white")
    x = 0
    for f in fr:
        out.paste(f, (x, 0))
        x += f.width + 20
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=85)
    return buf.getvalue()


# ---------- 9:16 preview ----------

def vertical(src: Path, dst: Path) -> None:
    """The whole frame on a blurred copy of itself, 540x960: the content is judged, not the crop."""
    p = ffmpeg(["-i", str(src), "-filter_complex",
                "[0]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,boxblur=18[bg];"
                "[0]scale=540:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p", str(dst)])
    if p.returncode:
        raise RuntimeError("preview failed: " + p.stderr.decode(errors="replace")[-200:])


# ---------- main ----------

def subjects_for(part: int) -> list[dict]:
    allsubs = sweep.load_subjects()
    niches = NICHES[:5] if part == 1 else NICHES[5:]
    out = []
    for n in niches:
        subs = [s for s in allsubs if s["niche"] == n]
        out += [subs[i] for i in PICK if i < len(subs)]
    return out


def labels(n: int, rng: random.Random) -> list[str]:
    """Random letters so old and new clips can't be told apart by name (the key is in made.json)."""
    letters = list(string.ascii_uppercase)
    rng.shuffle(letters)
    return letters[:n]


def run_subject(s: dict, lib, scorer, gates, keys: dict, tmp: Path, rng: random.Random, ask) -> dict:
    row = {"niche": s["niche"], "name": s["name"], "candidates": []}
    cands = candidates(s, lib, scorer, keys)
    good = [c for c in cands if "error" not in c]
    for c, lab in zip(good, labels(len(good), rng)):
        c["label"] = lab
    checks = []
    for c in cands:
        if "error" in c:
            row["candidates"].append(c)
            continue
        t0 = time.time()
        try:
            path = fetch_photo(c, tmp) if c.get("kind") == "photo" else fetch_video(c, s, scorer, tmp)
            secs = duration(str(path)) or CLIP
            frames = clip_frames(path, secs)
            c["gates"] = gates.check(frames)
            checks.append((c["label"], strip(frames), s["name"]))
            c["_path"] = path
            log(f"  {c['label']} {c['source']}: {c['fetch'].get('method')} {c['fetch'].get('seconds_taken')} s, "
                f"faults {c['gates']['faults'] or 'none'}")
        except Exception as e:
            c["error"] = str(e)[:300]
            c["seconds"] = round(time.time() - t0, 1)
            log(f"  {c['label']} {c['source']}: FAILED {str(e)[:160]}")
        row["candidates"].append(c)
    # blind check: one Claude call per subject (all its clips)
    if ask and checks:
        try:
            ok = ask(checks)
            for c in row["candidates"]:
                if "gates" in c:
                    c["claude_shows"] = c["label"] in ok
        except Exception as e:
            row["check_error"] = str(e)[:300]
    # previews for round 2: every clip that was made, named niche-subject-letter
    slug = "".join(ch if ch.isalnum() else "_" for ch in s["name"].lower()).strip("_")
    (OUT / "clips").mkdir(parents=True, exist_ok=True)
    for c in row["candidates"]:
        p = c.pop("_path", None)
        c.pop("_img", None)
        if p:
            # the landscape piece is kept for the crop research (R6b); .mkv so the runner doesn't publish it
            (OUT / "landscape").mkdir(parents=True, exist_ok=True)
            shutil.copy(p, OUT / "landscape" / f"{s['niche']}-{slug}-{c['label']}.mkv")
            dst = OUT / "clips" / f"{s['niche']}-{slug}-{c['label']}.mp4"
            try:
                vertical(p, dst)
                c["preview"] = dst.name
            except Exception as e:
                c["preview_error"] = str(e)[:200]
    return row


def summary(rows: list[dict]) -> dict:
    by = {}
    for r in rows:
        for c in r["candidates"]:
            b = by.setdefault(c["source"], {"tried": 0, "made": 0, "failed": 0, "claude_yes": 0, "faults": {},
                                            "seconds": [], "methods": {}})
            b["tried"] += 1
            if "gates" in c:
                b["made"] += 1
                b["claude_yes"] += bool(c.get("claude_shows"))
                b["seconds"].append(c["fetch"].get("seconds_taken") or 0)
                m = c["fetch"].get("method")
                b["methods"][m] = b["methods"].get(m, 0) + 1
                for f in c["gates"]["faults"]:
                    b["faults"][f] = b["faults"].get(f, 0) + 1
            else:
                b["failed"] += 1
    for b in by.values():
        s = sorted(b.pop("seconds"))
        b["median_seconds"] = s[len(s) // 2] if s else None
    return by


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", type=int, choices=(1, 2), default=1)
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    if RESEARCH_PY.exists() and Path(sys.prefix).resolve() != RESEARCH_PY.parent.parent.resolve() \
            and not os.environ.get("PIECES_NO_REEXEC"):
        os.execv(str(RESEARCH_PY), [str(RESEARCH_PY), "-m", "meta.collector.pieces", *(argv or sys.argv[1:])])
    try:
        os.nice(10)
    except OSError:
        pass
    OUT.mkdir(parents=True, exist_ok=True)
    subjects = subjects_for(a.part)[:a.limit] if a.limit else subjects_for(a.part)
    lib = sweep.open_library()
    keys = sweep.load_keys(lib[0]) if lib else {}
    scorer = sweep.Scorer(lib[0]) if lib else None
    gates = Gates(scorer)
    ask = None
    if sweep.ASK and lib:
        from shorts import llm
        from meta.common import verify
        settings = lib[0]
        ask = lambda items: verify(llm, settings, items)
    # Wikidata ids for the "depicts" photo search (Claude picks, as in the sweep)
    matches = []
    for s in subjects:
        try:
            matches.append(sweep.wikidata_matches(s["wikidata"]))
        except Exception as e:
            log(f"Wikidata lookup failed for {s['name']}: {e}")
            matches.append([])
    if sweep.ASK:
        try:
            picked = sweep.claude_qids(subjects, matches, sweep.ASK)
            for i, s in enumerate(subjects):
                s["qid"] = picked.get(i)
        except Exception as e:
            log(f"Wikidata pick failed: {e}")
    rng = random.Random(f"round2-part{a.part}")
    rows = []
    t_all = time.time()
    made = OUT / f"made-part{a.part}.json"
    with tempfile.TemporaryDirectory(prefix="pieces-") as tmpd:
        for i, s in enumerate(subjects, 1):
            log(f"{i}/{len(subjects)} {s['niche']} / {s['name']}")
            sub_tmp = Path(tmpd) / str(i)
            sub_tmp.mkdir()
            try:
                rows.append(run_subject(s, lib, scorer, gates, keys, sub_tmp, rng, ask))
            except Exception as e:  # never lose the run to one subject
                rows.append({"niche": s["niche"], "name": s["name"], "error": str(e)[:300]})
                log(f"  subject failed: {e}")
            shutil.rmtree(sub_tmp, ignore_errors=True)
            made.write_text(json.dumps({"part": a.part, "subjects": rows, "by_source": summary(rows),
                                        "minutes": round((time.time() - t_all) / 60, 1)}, indent=1, default=str),
                            encoding="utf-8")
    log(f"done: {len(rows)} subjects in {(time.time() - t_all) / 60:.0f} min")
    log("by source: " + json.dumps(summary(rows)))


if __name__ == "__main__":
    main()
