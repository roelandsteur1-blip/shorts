"""Footage collector research, step R6b: the best way to crop archive footage to 9:16.

Takes the landscape pieces the R4-R7 job kept (~/meta-out/pieces/landscape/), picks up to 2 good
video clips per subject (the blind check said they show the subject, no black or text faults) and
renders each clip with every crop method through the LIVE renderer's filter
(shorts/render._segment_filter), so a method's result is exactly what a Short would show.

Methods (letters are shuffled per clip on the review video; the key is in made.json):
    today     saliency + faces (shorts/framing.py), whole frame when it says so
    centre    the middle 9:16 strip
    whole     the whole frame on a blurred copy of itself
    band      the whole frame in a band in the middle (quiz/ranking layout)
    owlv2     OWLv2 (Apache-2.0) finds the named subject; the window follows its box
    gdino     Grounding DINO tiny (Apache-2.0), same
    claude    Claude picks one of 5 windows (or the whole frame) on the middle frame
    (detector + Claude is worked out afterwards from these, no extra calls)

Per clip it also measures, for every method, how much of the subject (the detectors' box) is
inside the window at each sampled frame, and the seconds each method took.

    python -m meta.collector.crops [--limit N]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import pieces, sweep

SRC = Path.home() / "meta-out" / "pieces"
OUT = Path.home() / "meta-out" / "crops"
PER_SUBJECT = 2
PARTS = 3
DETECT_EVERY = 2.0     # seconds between detector frames
MIN_SCORE = 0.12       # detector box score to count as "found"
PANEL_W, PANEL_H = 270, 480
METHODS = ["today", "centre", "whole", "band", "owlv2", "gdino", "claude"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- which clips ----------

def pick_clips(made: list[dict], landscape: Path) -> list[dict]:
    """Up to PER_SUBJECT video clips per subject that passed the blind check and have no black or
    text fault; new sources first, then the library."""
    out = []
    for part in made:
        for row in part.get("subjects", []):
            good = []
            for c in row.get("candidates", []):
                g = c.get("gates") or {}
                if c.get("kind") == "photo" or not c.get("claude_shows") or not c.get("preview"):
                    continue
                if {"black", "text on screen"} & set(g.get("faults", [])):
                    continue
                f = landscape / c["preview"].replace(".mp4", ".mkv")
                if f.exists():
                    good.append(dict(c, path=str(f), niche=row["niche"], name=row["name"]))
            good.sort(key=lambda c: (c["source"] == "library", -(c.get("thumb_score") or 0)))
            out += good[:PER_SUBJECT]
    return out


# ---------- detectors ----------

class Detector:
    """Open-vocabulary detector via transformers: boxes for the subject's name. None if it can't load."""

    def __init__(self, kind: str):
        self.kind, self.ok = kind, False
        try:
            import torch
            self.torch = torch
            if kind == "owlv2":
                from transformers import Owlv2ForObjectDetection, Owlv2Processor
                name = "google/owlv2-base-patch16-ensemble"
                self.proc, self.model = Owlv2Processor.from_pretrained(name), Owlv2ForObjectDetection.from_pretrained(name)
            else:
                from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
                name = "IDEA-Research/grounding-dino-tiny"
                self.proc, self.model = AutoProcessor.from_pretrained(name), AutoModelForZeroShotObjectDetection.from_pretrained(name)
            self.model.eval()
            self.ok = True
        except Exception as e:
            log(f"{kind} not available: {str(e)[:200]}")

    def box(self, im: Image.Image, text: str) -> tuple[list[float], float] | None:
        """Best box (x0, y0, x1, y1 as fractions) and its score, or None (also on any error)."""
        if not self.ok:
            return None
        try:
            return self._box(im, text)
        except Exception as e:
            if not getattr(self, "_warned", False):
                log(f"{self.kind} failed on a frame: {str(e)[:200]}")
                self._warned = True
            return None

    def _box(self, im: Image.Image, text: str):
        W, H = im.size
        with self.torch.no_grad():
            if self.kind == "owlv2":
                inp = self.proc(text=[[f"a photo of {text}"]], images=im, return_tensors="pt")
                out = self.model(**inp)
                side = max(W, H)  # OWLv2 pads to a square: boxes are relative to the padded side
                sizes = self.torch.tensor([[side, side]])
                # the name moved between transformers versions: try each spelling
                post = (getattr(self.proc, "post_process_object_detection", None)
                        or getattr(self.proc, "post_process_grounded_object_detection", None)
                        or self.proc.image_processor.post_process_object_detection)
                res = post(out, threshold=0.0, target_sizes=sizes)[0]
            else:
                inp = self.proc(images=im, text=f"{text.lower()}.", return_tensors="pt")
                out = self.model(**inp)
                try:
                    res = self.proc.post_process_grounded_object_detection(
                        out, inp.input_ids, threshold=0.0, text_threshold=0.0, target_sizes=[(H, W)])[0]
                except TypeError:  # older transformers call it box_threshold
                    res = self.proc.post_process_grounded_object_detection(
                        out, inp.input_ids, box_threshold=0.0, text_threshold=0.0, target_sizes=[(H, W)])[0]
        scores = res["scores"]
        if len(scores) == 0:
            return None
        i = int(scores.argmax())
        x0, y0, x1, y1 = [float(v) for v in res["boxes"][i]]
        return [max(0, x0 / W), max(0, y0 / H), min(1, x1 / W), min(1, y1 / H)], round(float(scores[i]), 3)


def detector_path(boxes: list, fw: float) -> dict | None:
    """Crop path from boxes over time: straight pan through the box centres; whole frame when the
    subject is wider than the window; None when nothing was found (caller falls back)."""
    found = [(t, b) for t, b in boxes if b and b[1] >= MIN_SCORE]
    if not found:
        return None
    xs = np.array([(b[0][0] + b[0][2]) / 2 for _, b in found])
    widths = np.array([b[0][2] - b[0][0] for _, b in found])
    whole = float(np.median(widths)) > fw * 1.1
    lo, hi = fw / 2, 1 - fw / 2
    if len(xs) >= 2:
        t = np.array([tt for tt, _ in found], dtype=float)
        t = (t - t.min()) / max(t.max() - t.min(), 1e-6)
        slope, icpt = np.polyfit(t, xs, 1)
        x0, x1 = float(icpt), float(icpt + slope)
    else:
        x0 = x1 = float(xs[0])
    return {"x0": float(np.clip(x0, lo, hi)), "x1": float(np.clip(x1, lo, hi)), "y0": 0.5, "y1": 0.5,
            "fy": 0.5, "whole": whole, "score": float(np.median([b[1] for _, b in found]))}


def reference_boxes(boxes: dict) -> list:
    """Per sampled frame, the higher-scoring of the two detectors' boxes: the subject's place."""
    lists = [v for v in boxes.values() if v]
    if not lists:
        return []
    out = []
    for k in range(max(len(v) for v in lists)):
        found = [v[k] for v in lists if k < len(v)]
        t = found[0][0]
        best = max((b for _, b in found if b), key=lambda b: b[1], default=None)
        out.append((t, best))
    return out


def coverage(path: dict, mode: str, boxes: list, fw: float) -> float | None:
    """Mean share of the subject box inside the window over the sampled frames."""
    refs = [(t, b[0]) for t, b in boxes if b and b[1] >= MIN_SCORE]
    if not refs:
        return None
    if mode in ("blur", "band"):
        return 1.0
    T = max(t for t, _ in refs) or 1.0
    shares = []
    for t, (x0, _, x1, _) in refs:
        cx = path["x0"] + (path["x1"] - path["x0"]) * (t / T)
        wl, wr = cx - fw / 2, cx + fw / 2
        inside = max(0.0, min(x1, wr) - max(x0, wl))
        shares.append(inside / max(x1 - x0, 1e-6))
    return round(float(np.mean(shares)), 3)


# ---------- Claude picks a window ----------

CLAUDE_PICK = """Each image is one frame of a video that will be cropped to a tall 9:16 phone frame.
Five numbered tall windows (1-5, left to right) are drawn on it; 6 means "show the whole frame
smaller, nothing cut off". For each image pick the option that shows the named subject best:
the whole subject if it fits, otherwise its most recognisable part; 6 only when every window
would cut off something essential.

{lines}

Return JSON only: {{"picks": [{{"image": 1, "pick": 3}}]}}"""


def windows(fw: float) -> list[float]:
    return [float(x) for x in np.linspace(fw / 2, 1 - fw / 2, 5)]


def window_sheet(im: Image.Image, fw: float) -> bytes:
    im = im.copy()
    d = ImageDraw.Draw(im)
    W, H = im.size
    colours = ["#ff4040", "#ffb000", "#40ff40", "#40c0ff", "#ff40ff"]
    for k, cx in enumerate(windows(fw)):
        x0, x1 = (cx - fw / 2) * W, (cx + fw / 2) * W
        d.rectangle([x0 + 2, 2 + k * 3, x1 - 2, H - 2 - k * 3], outline=colours[k], width=3)
        d.text((x0 + 8, 8 + k * 14), str(k + 1), fill=colours[k])
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def claude_picks(clips: list[dict], ask) -> dict[int, int]:
    """One call per 8 clips: {clip index: 1..6}."""
    out = {}
    for start in range(0, len(clips), 8):
        batch = clips[start:start + 8]
        lines = "\n".join(f"Image {i + 1}: {c['name']}" for i, c in enumerate(batch))
        try:
            data = ask(CLAUDE_PICK.format(lines=lines), [c["_sheet"] for c in batch])
            for p in data.get("picks", []):
                i, k = int(p.get("image", 0)) - 1, int(p.get("pick", 0))
                if 0 <= i < len(batch) and 1 <= k <= 6:
                    out[start + i] = k
        except Exception as e:
            log(f"Claude window pick failed: {str(e)[:200]}")
    return out


# ---------- rendering ----------

def render(src: str, seconds: float, w: int, h: int, method: str, frame: dict | None, dst: Path) -> None:
    from shorts import render as R
    if method == "band":
        flt = (f"[0:v]fps={R.FPS},scale={R.W}:-2,pad={R.W}:{R.H}:0:(oh-ih)/2:color=0x101010,"
               f"trim=end_frame={round(seconds * R.FPS)},setsar=1,format=yuv420p[v0]")
    else:
        mode = "blur" if method == "whole" or (frame or {}).get("whole") else "crop"
        flt = R._segment_filter(0, seconds, frame or {"x0": 0.5, "x1": 0.5, "y0": 0.5, "y1": 0.5}, w, h, mode)
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-filter_complex", flt,
                        "-map", "[v0]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", str(dst)],
                       capture_output=True, timeout=300)
    if p.returncode:
        raise RuntimeError(p.stderr.decode(errors="replace")[-300:])


def _pil_sizes() -> bool:
    """Pillow >= 10.1 draws text at a size without a font file."""
    try:
        ImageDraw.Draw(Image.new("RGB", (4, 4))).text((0, 0), "A", font_size=10)
        return True
    except TypeError:
        return False


def side_by_side(panels: list[tuple[str, Path]], dst: Path) -> None:
    """All methods in one 9:16-friendly grid video (2 rows x 4), labelled A-H only."""
    args, flt, n = [], [], len(panels)
    for i, (lab, p) in enumerate(panels):
        tag = p.with_suffix(".tag.png")
        im = Image.new("RGBA", (46, 40), (0, 0, 0, 190))
        ImageDraw.Draw(im).text((14, 8), lab, fill="white", font_size=24) if _pil_sizes() else \
            ImageDraw.Draw(im).text((18, 14), lab, fill="white")
        im.save(tag)
        args += ["-i", str(p), "-i", str(tag)]
        flt.append(f"[{2 * i}:v]scale={PANEL_W}:{PANEL_H}[s{i}];[s{i}][{2 * i + 1}:v]overlay=0:0[p{i}]")
    while len(flt) < 8:
        k = len(flt)
        flt.append(f"color=c=0x101010:s={PANEL_W}x{PANEL_H}:d=8[p{k}]")
    flt.append("".join(f"[p{i}]" for i in range(8)) +
               f"xstack=inputs=8:layout=0_0|w0_0|w0+w1_0|w0+w1+w2_0|0_h0|w0_h0|w0+w1_h0|w0+w1+w2_h0:fill=0x101010[out]")
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, "-filter_complex", ";".join(flt),
                        "-map", "[out]", "-t", "8", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                        "-pix_fmt", "yuv420p", str(dst)], capture_output=True, timeout=300)
    if p.returncode:
        raise RuntimeError(p.stderr.decode(errors="replace")[-300:])


# ---------- main ----------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--part", type=int, default=0, help="1-3: every third clip, so each part fits the runner's 3 hours")
    a = ap.parse_args(argv)
    if pieces.RESEARCH_PY.exists() and Path(sys.prefix).resolve() != pieces.RESEARCH_PY.parent.parent.resolve() \
            and not os.environ.get("PIECES_NO_REEXEC"):
        os.execv(str(pieces.RESEARCH_PY), [str(pieces.RESEARCH_PY), "-m", "meta.collector.crops", *(argv or sys.argv[1:])])
    try:
        os.nice(10)
    except OSError:
        pass
    from shorts import framing
    (OUT / "review").mkdir(parents=True, exist_ok=True)
    made = [json.loads(p.read_text()) for p in sorted(SRC.glob("made-part*.json"))]
    clips = pick_clips(made, SRC / "landscape")
    if a.part:
        clips = clips[a.part - 1::PARTS]
    if a.limit:
        clips = clips[:a.limit]
    log(f"{len(clips)} clips to crop")
    lib = sweep.open_library()
    ask = None
    if sweep.ASK and lib:
        from shorts import llm
        settings = lib[0]
        ask = lambda prompt, images: llm.ask_json(prompt, settings.claude_cmd, retries=1, images=images)
    dets = {k: Detector(k) for k in ("owlv2", "gdino")}
    rng = random.Random("crops-round2b")
    t_all = time.time()
    tmp = OUT / "tmp"
    tmp.mkdir(exist_ok=True)
    # 1) measure: frames, boxes, framing.py, window sheet for Claude
    for i, c in enumerate(clips, 1):
        secs = pieces.duration(c["path"]) or pieces.CLIP
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", c["path"]], capture_output=True, text=True)
        w, h = [int(x) for x in probe.stdout.strip().split(",")[:2]]
        c.update(seconds=secs, w=w, h=h, timing={})
        fw = framing.window_fraction(w, h)[0]
        times = list(np.arange(0.4, secs, DETECT_EVERY))
        frames = [(t, pieces.frame_at(c["path"], t, 960)) for t in times]
        frames = [(t, f) for t, f in frames if f]
        c["boxes"] = {}
        for k, d in dets.items():
            t0 = time.time()
            c["boxes"][k] = [(t, d.box(f, c["name"])) for t, f in frames] if d.ok else []
            c["timing"][k] = round(time.time() - t0, 1)
        t0 = time.time()
        c["frames_today"] = framing.subject_path(c["path"], 0.0, secs, w, h)
        c["timing"]["today"] = round(time.time() - t0, 1)
        mid = pieces.frame_at(c["path"], secs / 2, 960)
        c["_sheet"] = window_sheet(mid, fw) if mid else b""
        log(f"{i}/{len(clips)} {c['niche']} / {c['name']} ({c['source']}): owlv2 "
            f"{[b[1] if b else None for _, b in c['boxes']['owlv2']]} gdino {[b[1] if b else None for _, b in c['boxes']['gdino']]}")
    picks = claude_picks([c for c in clips if c["_sheet"]], ask) if ask else {}
    with_sheet = [c for c in clips if c["_sheet"]]
    for j, c in enumerate(with_sheet):
        c["claude_pick"] = picks.get(j)
    # 2) render every method, the review grid, and the coverage numbers
    rows = []
    for i, c in enumerate(clips, 1):
        fw = framing.window_fraction(c["w"], c["h"])[0]
        ref = reference_boxes(c["boxes"])
        paths = {"today": c["frames_today"], "centre": {"x0": 0.5, "x1": 0.5, "y0": 0.5, "y1": 0.5},
                 "whole": {"whole": True}, "band": None}
        for k in ("owlv2", "gdino"):
            paths[k] = detector_path(c["boxes"][k], fw) or dict(c["frames_today"], fallback=True)
        k = c.get("claude_pick")
        paths["claude"] = ({"whole": True} if k == 6 else
                           {"x0": windows(fw)[k - 1], "x1": windows(fw)[k - 1], "y0": 0.5, "y1": 0.5} if k else None)
        letters = list("ABCDEFGH")
        rng.shuffle(letters)
        key, panels, cov = {}, [], {}
        slug = c["preview"].replace(".mp4", "")
        for m, lab in zip(METHODS, letters):
            if m == "claude" and paths["claude"] is None:
                continue
            dst = tmp / f"{slug}-{m}.mp4"
            try:
                render(c["path"], c["seconds"], c["w"], c["h"], m, paths[m], dst)
                key[lab] = m
                panels.append((lab, dst))
                mode = "band" if m == "band" else "blur" if (paths[m] or {}).get("whole") or m == "whole" else "crop"
                cov[m] = coverage(paths[m] or {}, mode, ref, fw)
            except Exception as e:
                cov[m] = f"render failed: {str(e)[:120]}"
        panels.sort()
        review = OUT / "review" / f"crop-{slug}.mp4"
        try:
            side_by_side(panels, review)
        except Exception as e:
            log(f"  grid failed: {e}")
        rows.append({k2: v for k2, v in c.items() if not k2.startswith("_") and k2 not in ("frames_today",)} |
                    {"key": key, "coverage": cov, "paths": paths, "review": review.name})
        for p in tmp.glob(f"{slug}-*.mp4"):
            p.unlink()
        (OUT / (f"made-part{a.part}.json" if a.part else "made.json")).write_text(json.dumps({"clips": rows, "minutes": round((time.time() - t_all) / 60, 1)},
                                                  indent=1, default=str), encoding="utf-8")
        log(f"  {i}/{len(clips)} rendered; subject inside window: {json.dumps(cov)}")
    log(f"done: {len(rows)} clips in {(time.time() - t_all) / 60:.0f} min")


if __name__ == "__main__":
    main()
