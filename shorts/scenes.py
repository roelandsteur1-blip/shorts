"""Cut downloaded videos into scenes, score them and pick a 9:16 crop position."""
from __future__ import annotations

import logging
import multiprocessing
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np

from . import progress
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


def analyse_video(asset_id: int, path: str, duration: float, kf_dir: str) -> list[dict]:
    """Cut and score one video. Pure (no database), so it can run in a worker process."""
    cv2.setNumThreads(1)  # parallelism comes from running several videos at once
    cuts = split_long(detect_cuts(path), duration or 0)
    cap = cv2.VideoCapture(path)
    out_dir = Path(kf_dir) / str(asset_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    found = []
    try:
        for start, end in cuts:
            times = np.linspace(start + 0.1, end - 0.1, SAMPLES)
            frames = [f for f in (_read_frame(cap, t) for t in times) if f is not None]
            if len(frames) < 2:
                continue
            score = score_frames(frames)
            kf = out_dir / f"{start:08.2f}.jpg"
            cv2.imwrite(str(kf), frames[len(frames) // 2], [cv2.IMWRITE_JPEG_QUALITY, 88])
            found.append({"start": start, "end": end, "keyframe": str(kf), **score,
                          "reason": reject_reason(score)})
    finally:
        cap.release()
    return found


def save_scenes(conn: sqlite3.Connection, asset_id: int, found: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO scenes (asset_id, start, end, keyframe, motion, contrast, brightness,
                               text_like, crop_x, rejected, reject_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(asset_id, f["start"], f["end"], f["keyframe"], f["motion"], f["contrast"], f["brightness"],
          f["text_like"], f["crop_x"], int(f["reason"] is not None), f["reason"]) for f in found],
    )
    conn.execute("UPDATE assets SET status='scened' WHERE id=?", (asset_id,))
    conn.commit()


def process_asset(conn: sqlite3.Connection, settings: Settings, asset: sqlite3.Row) -> int:
    found = analyse_video(asset["id"], asset["path"], asset["duration"], str(settings.keyframe_dir))
    save_scenes(conn, asset["id"], found)
    return len(found)


def process_all(conn: sqlite3.Connection, settings: Settings, workers: int | None = None) -> int:
    """Analyse all downloaded videos in parallel (one per CPU core, minus one to keep the
    laptop usable); only this process writes to the database."""
    rows = [r for r in conn.execute("SELECT * FROM assets WHERE status='downloaded'").fetchall()
            if r["path"] and Path(r["path"]).exists()]
    if not rows:
        return 0
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    total = 0
    # "spawn" everywhere (the Windows default): forking a process that already loaded OpenCV
    # or PyTorch threads can deadlock on Linux.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=min(workers, len(rows)), mp_context=ctx) as pool:
        futures = {pool.submit(analyse_video, r["id"], r["path"], r["duration"],
                               str(settings.keyframe_dir)): r for r in rows}
        for done, fut in enumerate(as_completed(futures), 1):
            asset = futures[fut]
            try:
                found = fut.result()
                save_scenes(conn, asset["id"], found)
                total += len(found)
                log.info("[%d/%d] %s: %d scenes", done, len(rows), asset["source_id"], len(found))
                progress.report(done, len(rows), "cut scenes")
            except Exception as e:  # noqa: BLE001
                log.warning("scene detection failed for %s: %s", asset["source_id"], e)
                conn.execute("UPDATE assets SET status='failed', error=? WHERE id=?",
                             (str(e)[:500], asset["id"]))
                conn.commit()
    return total
