"""The right part of a good clip: which seconds (W) and which 9:16 crop (S) each method picks.

Clips: per block the on-topic candidate (judge topic 2) with the most interest that is longer
than the block, so there is a choice of seconds. Every method answers on the same clips; then
one Claude call per clip sees (1) the clip every half second, numbered, and says per frame whether
the block's subject is in view and where the best part starts, and (2) every distinct crop the
methods made of the clip's middle frame, lettered in random order, and rates each."""
from __future__ import annotations

import logging
import random
import time

import cv2
import numpy as np

from .. import framing, llm
from . import SEED, load, root, save, status_line
from .detect import box_to_crop, load_finder
from .frames import dense, read_at
from .judge import FH, FW, ask, grid, jpeg, label_box, pace, strip
from .models import MODELS, Encoder, emb_path

log = logging.getLogger(__name__)

STEP = 0.5
MAIN = "siglip2-b16"   # the model the combined seconds methods use (falls back to the first embedded)

CLIP_PROMPT = """A YouTube Short shows one part of a video clip while the on-screen text says: "{text}"
The writer asked for footage of: "{visual}" (the Short is about: {topic})
The part shown lasts {seconds:.1f} seconds.

Image 1 shows the clip every half second, frames numbered 1 to {n} in time order.
For every frame say how well the subject the text talks about can be seen: \
2 = clearly in view, 1 = partly, small or unclear, 0 = not in view (or black, blurry, a title card).
Then give the frame number where the best {seconds:.1f}-second part starts (the subject in view the \
whole time, the most interesting moment).

Image 2 shows at the top the whole frame (FULL) and below it tall 9:16 crops of that same frame, \
lettered. A crop that shows the whole frame with black bars above and below means "show it whole". \
Rate every letter: 2 = the subject the text talks about is fully in the crop and well placed; \
1 = partly cut off, very small, or off to one side; 0 = the subject is not in the crop.
Then give the letter you would show viewers.

Answer with only JSON: {{"frames": {{"1": 0, "2": 2, ...}}, "best_start": 1, "crops": {{"A": 2, ...}}, \
"best_crop": "A"}}"""


def choose(ts: dict, labels: dict, n: int = 60) -> list[dict]:
    """Up to n (block, clip) pairs: a clearly on-topic clip with room to choose seconds."""
    rng = random.Random(SEED)
    out = []
    for b in ts["blocks"]:
        got = labels.get(str(b["id"]), {})
        good = []
        for sid, (topic, interest) in got.items():
            s = ts["scenes"].get(sid)
            if topic == 2 and s and s["end"] - s["start"] >= (b["seconds"] or 2.5) + 1.5:
                good.append((interest, rng.random(), sid))
        if good:
            sid = max(good)[2]
            out.append({"block": b, "sid": sid, "scene": ts["scenes"][sid]})
    rng.shuffle(out)
    return out[:n]


def _windows(times: list[float], end: float, length: float) -> list[int]:
    """Frame indexes a window of `length` seconds can start at."""
    return [i for i, t in enumerate(times) if t + length <= end + 0.01] or [0]


def _pick(times, end, length, per_frame: np.ndarray, allowed: np.ndarray | None = None) -> float:
    starts = _windows(times, end, length)
    best, best_v = starts[0], -np.inf
    for i in starts:
        inside = [j for j, t in enumerate(times) if times[i] <= t < times[i] + length] or [i]
        if allowed is not None and not allowed[inside].all():
            continue
        v = float(np.mean(per_frame[inside]))
        if v > best_v:
            best, best_v = i, v
    return times[best]


def _motion(frames: list[np.ndarray]) -> np.ndarray:
    grays = [cv2.cvtColor(cv2.resize(f, (160, 90)), cv2.COLOR_BGR2GRAY).astype(np.float32) / 255 for f in frames]
    m = [float(np.mean(np.abs(a - b))) for a, b in zip(grays[1:], grays[:-1])]
    return np.array([m[0] if m else 0.0] + m)


def _clean(frames: list[np.ndarray]) -> np.ndarray:
    """False for frames no viewer should get: near black, very blurry, or a flat title card."""
    ok = []
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        sharp = cv2.Laplacian(g, cv2.CV_64F).var()
        flat = float((np.abs(g.astype(np.float32) - np.median(g)) < 10).mean())
        ok.append(g.mean() > 8 and sharp > 15 and flat < 0.85)
    return np.array(ok)


def _z(v: np.ndarray) -> np.ndarray:
    return (v - v.mean()) / (v.std() + 1e-6)


def measure(settings, ts: dict, labels: dict, n: int, threads: int, finders: list[str]) -> dict:
    path = root(settings) / "clips.json"
    data = load(path, {})
    picks = choose(ts, labels, n)
    # Dense frames once per clip (kept in memory: 60 clips x 16 small frames).
    dense_frames = {}
    for i, p in enumerate(picks):
        key = str(p["block"]["id"])
        s = p["scene"]
        dense_frames[key] = dense(s["path"], s["start"], s["end"], STEP)
        d = data.setdefault(key, {"sid": int(p["sid"]), "seconds": p["block"]["seconds"] or 2.5,
                                  "w": {}, "s": {}})
        d["times"] = [t for t, _ in dense_frames[key]]
        status_line(settings, "clips: read frames", i + 1, len(picks))
    models = [m for m in MODELS if emb_path(settings, m).exists()]
    main = MAIN if MAIN in models else (models[0] if models else None)
    sims: dict[str, dict[str, np.ndarray]] = {}
    for m in models:
        enc = Encoder(m, threads)
        for key, fr in dense_frames.items():
            if not fr:
                continue
            b = next(p["block"] for p in picks if str(p["block"]["id"]) == key)
            t0 = time.perf_counter()
            v = enc.arrays([f for _, f in fr])
            q = enc.texts([b["visual"]])[0]
            sims.setdefault(key, {})[m] = v @ q
            data[key]["w"].setdefault(f"match:{m}", {})["secs"] = time.perf_counter() - t0
        crop_methods = {"today": "clip-b32"} if m == "clip-b32" else {f"guide:{m}": m}
        for name in crop_methods:
            for j, p in enumerate(picks):
                key = str(p["block"]["id"])
                if name in data[key]["s"]:
                    continue
                data[key]["s"][name] = _crop_framing(p, Padded(enc))
                status_line(settings, f"crops {name}", j + 1, len(picks))
        save(path, data)
        del enc
    for p in picks:
        key = str(p["block"]["id"])
        fr = dense_frames.get(key) or []
        if not fr:
            continue
        d, s = data[key], p["scene"]
        times, end, length = d["times"], s["end"], d["seconds"]
        frames_only = [f for _, f in fr]
        motion, clean = _motion(frames_only), _clean(frames_only)
        d["w"]["start"] = {"start": times[0]}
        mid = (s["start"] + s["end"] - length) / 2
        d["w"]["middle"] = {"start": times[min(_windows(times, end, length), key=lambda i: abs(times[i] - mid))]}
        d["w"]["motion"] = {"start": _pick(times, end, length, motion)}
        for m, sv in sims.get(key, {}).items():
            d["w"][f"match:{m}"]["start"] = _pick(times, end, length, sv)
            d["w"][f"match-clean:{m}"] = {"start": _pick(times, end, length, sv, clean)
                                          if clean.any() else _pick(times, end, length, sv)}
        if main and main in sims.get(key, {}):
            combo = _z(sims[key][main]) + 0.5 * _z(motion)
            d["w"][f"match+motion:{main}"] = {"start": _pick(times, end, length, combo, clean if clean.any() else None)}
        if "centre" not in d["s"]:
            d["s"]["centre"] = {"x": 0.5, "whole": False, "secs": 0.0}
        if "saliency" not in d["s"]:
            d["s"]["saliency"] = _crop_framing(p, None)
    save(path, data)
    for name in finders:
        if all(name in data[str(p["block"]["id"])]["s"] for p in picks):
            continue
        finder = load_finder(name)
        if finder is None:
            continue
        for j, p in enumerate(picks):
            key = str(p["block"]["id"])
            if name not in data[key]["s"]:
                data[key]["s"][name] = _crop_finder(p, finder)
            status_line(settings, f"crops {name}", j + 1, len(picks))
        save(path, data)
        del finder
    return data


class Padded:
    """The encoder as framing.text_guide gets it in production: crops padded to a square, not cut."""

    def __init__(self, enc: Encoder):
        self.enc = enc

    def arrays(self, images):
        return self.enc.arrays(images, pad=True)

    def texts(self, texts):
        return self.enc.texts(texts)


def _middle(p: dict) -> tuple[float, float]:
    s, length = p["scene"], p["block"]["seconds"] or 2.5
    start = max(s["start"], (s["start"] + s["end"] - length) / 2)
    return start, min(length, s["end"] - start)


def _crop_framing(p: dict, enc) -> dict:
    s = p["scene"]
    start, length = _middle(p)
    t0 = time.perf_counter()
    res = framing.subject_path(s["path"], start, length, s["width"] or 1920, s["height"] or 1080,
                               query=p["block"]["visual"] if enc is not None else None, encoder=enc)
    return {"x": (res["x0"] + res["x1"]) / 2, "whole": bool(res.get("whole")),
            "secs": time.perf_counter() - t0}


def _crop_finder(p: dict, finder) -> dict:
    s = p["scene"]
    start, length = _middle(p)
    frames = framing.read_frames(s["path"], start, length, s["width"] or 1920, s["height"] or 1080)
    frames = [frames[int(i)] for i in np.linspace(0, len(frames) - 1, min(3, len(frames)))] if frames else []
    fw, _ = framing.window_fraction(s["width"] or 1920, s["height"] or 1080)
    t0 = time.perf_counter()
    box = finder.find(frames, p["block"]["visual"]) if frames else None
    secs = time.perf_counter() - t0
    if box is None:  # nothing found: what the pipeline would do without it
        out = _crop_framing(p, None)
        out.update(found=False, secs=secs + out["secs"])
        return out
    return {**box_to_crop(box, fw), "found": True, "secs": secs}


def _crop_tile(frame: np.ndarray, crop: dict, fw: float, height: int = 320) -> np.ndarray:
    h, w = frame.shape[:2]
    tw = round(height * 9 / 16)
    tile = np.zeros((height, tw, 3), np.uint8)
    if crop.get("whole") or fw >= 1:
        s = tw / w
        nh = round(h * s)
        tile[(height - nh) // 2:(height - nh) // 2 + nh] = cv2.resize(frame, (tw, nh), interpolation=cv2.INTER_AREA)
        return tile
    cw = max(1, round(fw * w))
    x0 = int(np.clip(round(crop["x"] * w - cw / 2), 0, w - cw))
    return cv2.resize(frame[:, x0:x0 + cw], (tw, height), interpolation=cv2.INTER_AREA)


def _letters(methods: dict) -> dict[str, list[str]]:
    """Methods that made (nearly) the same crop share a letter: fewer tiles, same verdict."""
    groups: list[tuple[dict, list[str]]] = []
    for name, c in methods.items():
        if "x" not in c:
            continue
        for g, names in groups:
            if g.get("whole") == c.get("whole") and (c.get("whole") or abs(g["x"] - c["x"]) < 0.03):
                names.append(name)
                break
        else:
            groups.append((c, [name]))
    rng = random.Random(SEED)
    rng.shuffle(groups)
    return {chr(65 + i): names for i, (_, names) in enumerate(groups)}


def run_judge(settings, ts: dict, limit: int | None = None, every: float = 0.0) -> dict:
    path = root(settings) / "clips.json"
    data = load(path, {})
    blocks = {str(b["id"]): b for b in ts["blocks"]}
    todo = [k for k, d in data.items() if "judge" not in d and d.get("times")]
    if limit is not None:
        todo = todo[:limit]
    last: list[float] = []
    waited = [0.0]
    for n, key in enumerate(todo):
        d, b = data[key], blocks[key]
        s = ts["scenes"][str(d["sid"])]
        fr = dense(s["path"], s["start"], s["end"], STEP)
        tiles = [strip([f], str(i + 1)) for i, (_, f) in enumerate(fr)]
        sheet1 = grid(tiles, 6) if tiles else np.zeros((FH, FW, 3), np.uint8)
        letters = _letters(d["s"])
        start, _ = _middle({"scene": s, "block": b})
        cap = cv2.VideoCapture(s["path"])
        frame = read_at(cap, start + min(d["seconds"], s["end"] - start) / 2, 768)
        cap.release()
        if frame is None:
            continue
        fw, _ = framing.window_fraction(s["width"] or 1920, s["height"] or 1080)
        full = cv2.resize(frame, (576, round(frame.shape[0] * 576 / frame.shape[1])))
        label_box(full, "FULL")
        crops = []
        for letter, names in letters.items():
            t = _crop_tile(frame, d["s"][names[0]], fw)
            label_box(t, letter, 0.9)
            crops.append(t)
        row = grid(crops, len(crops)) if crops else np.zeros((10, 10, 3), np.uint8)
        width = max(full.shape[1], row.shape[1])
        sheet2 = np.full((full.shape[0] + 10 + row.shape[0], width, 3), 70, np.uint8)
        sheet2[:full.shape[0], :full.shape[1]] = full
        sheet2[full.shape[0] + 10:, :row.shape[1]] = row
        prompt = CLIP_PROMPT.format(text=b["text"], visual=b["visual"], topic=b["topic"] or "?",
                                    seconds=d["seconds"], n=len(fr))
        status_line(settings, "judge clips", n, len(todo), f"block {key}")
        pace(every, last)
        try:
            ans = ask(settings, prompt, [jpeg(sheet1), jpeg(sheet2)], waited)
        except llm.LLMLimitError:
            log.warning("Claude usage limit for a day: judging stops here; run again after it resets")
            break
        except llm.LLMError as e:
            log.warning("clip judge failed for block %s: %s", key, e)
            continue
        if not isinstance(ans, dict):
            continue
        d["judge"] = {"frames": {k: int(v) for k, v in (ans.get("frames") or {}).items()},
                      "best_start": ans.get("best_start"), "crops": {k: int(v) for k, v in (ans.get("crops") or {}).items()},
                      "best_crop": ans.get("best_crop"), "letters": letters, "frame_times": [t for t, _ in fr]}
        save(path, data)
    return data

