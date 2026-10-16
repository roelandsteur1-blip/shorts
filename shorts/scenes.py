"""Cut downloaded videos into scenes, score them and pick a 9:16 crop position."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import cv2
import numpy as np

from .config import Settings

log = logging.getLogger(__name__)

MIN_SCENE = 1.5      # seconds; shorter scenes are dropped
MAX_SCENE = 8.0      # longer scenes are split into chunks so each can be used separately
SAMPLES = 6          # frames sampled per scene for scoring


def detect_cuts(path: str) -> list[tuple[float, float]]:
    from scenedetect import ContentDetector, detect

    found = detect(path, ContentDetector(threshold=27.0), show_progress=False)
    sec = (lambda t: t.seconds) if hasattr(found[0][0] if found else None, "seconds") else (lambda t: t.get_seconds())
    return [(sec(a), sec(b)) for a, b in found]


def split_long(cuts: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    if not cuts:
        cuts = [(0.0, duration)]
    out = []
    for start, end in cuts:
        length = end - start
        if length < MIN_SCENE:
            continue
        n = max(1, int(np.ceil(length / MAX_SCENE)))
        step = length / n
        for i in range(n):
            out.append((start + i * step, start + (i + 1) * step))
    return out


def _read_frame(cap: cv2.VideoCapture, t: float) -> np.ndarray | None:
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    return frame if ok else None


def score_frames(frames: list[np.ndarray]) -> dict:
    """Motion, contrast, brightness, 'looks like a title card', and horizontal crop centre."""
    small = [cv2.resize(f, (320, 180)) for f in frames]
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255 for f in small]
    motion = float(np.mean([np.mean(np.abs(a - b)) for a, b in zip(grays, grays[1:])])) if len(grays) > 1 else 0.0
    mid = grays[len(grays) // 2]
    contrast = float(mid.std())
    brightness = float(mid.mean())

    edges = cv2.Canny((mid * 255).astype(np.uint8), 100, 200) > 0
    edge_density = float(edges.mean())
    # Title cards / slides: almost no motion, lots of sharp edges, flat background.
    flat_bg = float((np.abs(mid - np.median(mid)) < 0.04).mean())
    text_like = edge_density * flat_bg if motion < 0.01 else 0.0

    # Crop centre: where the detail is (gradient energy), smoothed toward the middle.
    gx = cv2.Sobel(mid, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(mid, cv2.CV_32F, 0, 1)
    energy = np.sqrt(gx ** 2 + gy ** 2).sum(axis=0)
    if energy.sum() > 0:
        cx = float((energy * np.arange(energy.size)).sum() / energy.sum() / energy.size)
    else:
        cx = 0.5
    crop_x = 0.5 + 0.7 * (cx - 0.5)
    return {"motion": motion, "contrast": contrast, "brightness": brightness,
            "text_like": text_like, "crop_x": float(np.clip(crop_x, 0.0, 1.0))}


def reject_reason(score: dict) -> str | None:
    if score["brightness"] < 0.03:
        return "black"
    if score["contrast"] < 0.02:
        return "flat"
    if score["text_like"] > 0.05:
        return "title card"
    return None


def process_asset(conn: sqlite3.Connection, settings: Settings, asset: sqlite3.Row) -> int:
    path = asset["path"]
    cuts = split_long(detect_cuts(path), asset["duration"] or 0)
    cap = cv2.VideoCapture(path)
    kf_dir = settings.keyframe_dir / str(asset["id"])
    kf_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        for start, end in cuts:
            times = np.linspace(start + 0.1, end - 0.1, SAMPLES)
            frames = [f for f in (_read_frame(cap, t) for t in times) if f is not None]
            if len(frames) < 2:
                continue
            score = score_frames(frames)
            kf = kf_dir / f"{start:08.2f}.jpg"
            cv2.imwrite(str(kf), frames[len(frames) // 2], [cv2.IMWRITE_JPEG_QUALITY, 88])
            reason = reject_reason(score)
            conn.execute(
                """INSERT INTO scenes (asset_id, start, end, keyframe, motion, contrast, brightness,
                                       text_like, crop_x, rejected, reject_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (asset["id"], start, end, str(kf), score["motion"], score["contrast"],
                 score["brightness"], score["text_like"], score["crop_x"], int(reason is not None), reason),
            )
            n += 1
    finally:
        cap.release()
    conn.execute("UPDATE assets SET status='scened' WHERE id=?", (asset["id"],))
    conn.commit()
    return n


def process_all(conn: sqlite3.Connection, settings: Settings) -> int:
    rows = conn.execute("SELECT * FROM assets WHERE status='downloaded'").fetchall()
    total = 0
    for asset in rows:
        if not asset["path"] or not Path(asset["path"]).exists():
            continue
        try:
            n = process_asset(conn, settings, asset)
            log.info("%s: %d scenes", asset["source_id"], n)
            total += n
        except Exception as e:  # noqa: BLE001
            log.warning("scene detection failed for %s: %s", asset["source_id"], e)
            conn.execute("UPDATE assets SET status='failed', error=? WHERE id=?", (str(e)[:500], asset["id"]))
            conn.commit()
    return total
