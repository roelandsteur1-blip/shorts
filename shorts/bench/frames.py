"""Frames read from the videos for the bench: 3 per library scene, or every half second of a clip."""
from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from . import root, status_line

log = logging.getLogger(__name__)

SPOTS = (0.15, 0.5, 0.85)  # where in a scene its 3 frames are taken
WIDTH = 384                 # same width as the pipeline's keyframes


def frame_path(settings, sid: int | str, k: int) -> Path:
    return root(settings) / "frames" / f"{sid}_{k}.jpg"


def read_at(cap: cv2.VideoCapture, t: float, width: int = WIDTH) -> np.ndarray | None:
    cap.set(cv2.CAP_PROP_POS_MSEC, max(t, 0) * 1000)
    ok, frame = cap.read()
    if not ok or frame is None or not frame.size:
        return None
    h, w = frame.shape[:2]
    return cv2.resize(frame, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)


def _one_video(job: tuple[str, list[tuple[int, float, float]], str]) -> int:
    path, scenes, out = job
    cap = cv2.VideoCapture(path)
    done = 0
    try:
        for sid, start, end in scenes:
            for k, f in enumerate(SPOTS):
                dst = Path(out) / f"{sid}_{k}.jpg"
                if dst.exists():
                    continue
                img = read_at(cap, start + (end - start) * f)
                if img is not None:
                    cv2.imwrite(str(dst), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
            done += 1
    finally:
        cap.release()
    return done


def extract(settings, ts: dict, workers: int = 4) -> None:
    out = root(settings) / "frames"
    out.mkdir(exist_ok=True)
    jobs: dict[str, list] = defaultdict(list)
    for sid, s in ts["scenes"].items():
        if not all(frame_path(settings, sid, k).exists() for k in range(len(SPOTS))):
            jobs[s["path"]].append((int(sid), s["start"], s["end"]))
    total, done = sum(len(v) for v in jobs.values()), 0
    if not total:
        return
    log.info("frames: %d scenes in %d videos", total, len(jobs))
    with ProcessPoolExecutor(workers) as ex:
        for n in ex.map(_one_video, [(p, sorted(v, key=lambda x: x[1]), str(out)) for p, v in jobs.items()]):
            done += n
            status_line(settings, "frames", done, total)


def load_frames(settings, sid: int | str) -> list[np.ndarray | None]:
    out = []
    for k in range(len(SPOTS)):
        img = cv2.imread(str(frame_path(settings, sid, k)))
        out.append(img if img is not None and img.size else None)
    return out


def dense(path: str, start: float, end: float, step: float = 0.5, width: int = WIDTH) -> list[tuple[float, np.ndarray]]:
    """(time, frame) every `step` seconds of a scene."""
    cap = cv2.VideoCapture(path)
    out = []
    try:
        for t in np.arange(start, end - 0.05, step):
            img = read_at(cap, float(t), width)
            if img is not None:
                out.append((float(t), img))
    finally:
        cap.release()
    return out
