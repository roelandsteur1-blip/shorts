"""Cut downloaded videos into scenes, score them and pick a 9:16 crop position."""
from __future__ import annotations

import logging
import multiprocessing
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
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


# ---------- fast detector: one ffmpeg pass, small frames, no seeking ----------

FRAME_W = 384          # frames are read at this width (keyframes too; CLIP looks at 224 px)
DETECT_W = 192         # cut detection works on a half-size copy
CUT_THRESHOLD = 27.0   # same scale as PySceneDetect's ContentDetector (mean HSV change, 0-255)
ADAPTIVE = 3.0        # ...and at least this many times the average change of the last second
MIN_CUT_GAP = 0.6      # seconds; a flash or a fast pan doesn't count as two cuts
KEEP_FPS = 4.0         # frames per second kept in memory for scoring and keyframes
MAX_KEPT = 1200        # a very long shot keeps every other frame beyond this (memory cap)
CHUNK_OVER = 300.0     # videos longer than this (s) are split over several workers...
CHUNK_SECONDS = 180.0  # ...in ranges of this length


def _frame_height(width: int, height: int) -> int:
    h = round(FRAME_W * (height or 9) / max(width or 16, 1) / 2) * 2
    return max(2, h)


def _ffmpeg_frames(path: str, start: float, length: float, fps: float, w: int, h: int, decoder: str):
    """Yield BGR frames (h x w) from ffmpeg, `fps` per second, from `start` for `length` seconds."""
    import subprocess
    if decoder == "cuda":
        pre = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        vf = f"fps={fps},scale_cuda={w}:{h},hwdownload,format=nv12,format=bgr24"
    else:
        pre = ["-threads", "2"]
        vf = f"fps={fps},scale={w}:{h}:flags=area,format=bgr24"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *pre, "-ss", f"{start:.3f}",
           "-t", f"{length:.3f}", "-i", path, "-an", "-sn", "-vf", vf,
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    size = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg ({decoder}) failed on {path}: {err.strip()[-500:]}")


def _finish_shot(t0: float, t1: float, kept: list[tuple[float, np.ndarray]], out_dir: Path) -> list[dict]:
    """Chunk one shot like split_long and score each chunk from the frames kept in memory."""
    found = []
    for start, end in split_long([(t0, t1)], t1):
        inside = [f for t, f in kept if start + 0.1 <= t <= end - 0.1] or [f for t, f in kept if start <= t <= end]
        if len(inside) < 2:
            continue
        frames = [inside[int(i)] for i in np.linspace(0, len(inside) - 1, min(SAMPLES, len(inside)))]
        score = score_frames(frames)
        kf = out_dir / f"{start:08.2f}.jpg"
        cv2.imwrite(str(kf), frames[len(frames) // 2], [cv2.IMWRITE_JPEG_QUALITY, 90])
        found.append({"start": start, "end": end, "keyframe": str(kf), **score, "reason": reject_reason(score)})
    return found


def analyse_range(asset_id: int, path: str, start: float, end: float, width: int, height: int,
                  kf_dir: str, fps: float = 10.0, decoder: str = "cpu") -> list[dict]:
    """Cut and score [start, end) of one video in a single decoding pass: small frames at `fps`,
    a cut where the mean HSV change jumps over CUT_THRESHOLD, scoring and keyframes from frames
    already in memory. Pure (no database), runs in a worker process. A range edge counts as a cut."""
    cv2.setNumThreads(1)
    out_dir = Path(kf_dir) / str(asset_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = FRAME_W, _frame_height(width, height)
    keep_every = max(1, round(fps / KEEP_FPS))
    try:
        return _scan(path, start, end, w, h, fps, decoder, keep_every, out_dir)
    except RuntimeError:
        if decoder == "cpu":
            raise
        return _scan(path, start, end, w, h, fps, "cpu", keep_every, out_dir)  # GPU decode failed: CPU


def _scan(path, start, end, w, h, fps, decoder, keep_every, out_dir) -> list[dict]:
    found: list[dict] = []
    prev = None
    shot_start, last_cut = start, start - MIN_CUT_GAP
    kept: list[tuple[float, np.ndarray]] = []
    stride = keep_every
    count = 0
    window: list[float] = []
    for n, frame in enumerate(_ffmpeg_frames(path, start, end - start, fps, w, h, decoder)):
        count = n + 1
        t = start + n / fps
        small = cv2.cvtColor(cv2.resize(frame, (DETECT_W, max(2, h * DETECT_W // w)),
                                        interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2HSV).astype(np.int16)
        if prev is not None:
            delta = float(np.abs(small - prev).mean())  # mean over H, S and V
            # A cut is a jump well above the change of the last second: busy footage (fire, a
            # fast pan, flickering screens) changes a lot every frame without being a cut.
            recent = float(np.mean(window)) if window else 0.0
            if delta >= CUT_THRESHOLD and delta >= ADAPTIVE * recent and t - last_cut >= MIN_CUT_GAP:
                found += _finish_shot(shot_start, t, kept, out_dir)
                shot_start, last_cut, kept, stride = t, t, [], keep_every
                window.clear()
            else:
                window.append(delta)
                if len(window) > max(2, int(fps)):
                    window.pop(0)
        prev = small
        if n % stride == 0:
            kept.append((t, frame.copy()))
            if len(kept) > MAX_KEPT:
                kept, stride = kept[::2], stride * 2
    stop = min(end, start + count / fps)
    if stop > shot_start:
        found += _finish_shot(shot_start, stop, kept, out_dir)
    return found


def ranges_for(duration: float) -> list[tuple[float, float]]:
    """Long videos are analysed in parts by several workers at once (no slow tail)."""
    if not duration or duration <= CHUNK_OVER:
        return [(0.0, duration or 1e9)]
    n = int(np.ceil(duration / CHUNK_SECONDS))
    step = duration / n
    return [(i * step, (i + 1) * step) for i in range(n)]


def process_all(conn: sqlite3.Connection, settings: Settings, workers: int | None = None,
                more_coming=None) -> int:
    """Analyse all downloaded videos in parallel (longest first, long ones split into parts) at
    low priority; how many run at once follows the dashboard's Quiet/Fast switch. Only this process
    writes to the database. more_coming(): True while downloads still run (restock); videos that
    finish downloading meanwhile join the queue, so cutting starts with the first download."""
    from . import media, speed, timing
    import time
    from concurrent.futures import FIRST_COMPLETED, wait
    fast = settings.scene_detector != "pyscenedetect"
    decoder = "cuda" if fast and media.nvdec_works() else "cpu"
    seen: set[int] = set()
    queue: list[tuple] = []  # (asset row, start, end), biggest part first
    parts: dict[int, int] = {}
    results: dict[int, list[dict]] = {}
    began: dict[int, float] = {}
    failed: set[int] = set()

    def add_new() -> None:
        rows = [r for r in conn.execute("SELECT * FROM assets WHERE status='downloaded'").fetchall()
                if r["id"] not in seen and r["path"] and Path(r["path"]).exists()]
        for r in rows:
            seen.add(r["id"])
            ranges = ranges_for(r["duration"] or 0) if fast else [(0.0, r["duration"] or 0)]
            parts[r["id"]], results[r["id"]] = len(ranges), []
            queue.extend((r, start, end) for start, end in ranges)
        queue.sort(key=lambda t: -(min(t[2], 1e6) - t[1]))

    add_new()
    if not queue and not (more_coming and more_coming()):
        return 0
    gate = speed.Gate(settings.data_dir, "scene_workers")
    max_workers = workers or max(speed.limits(settings.data_dir).scene_workers, os.cpu_count() or 2)
    total, done_assets = 0, 0
    # "spawn" everywhere (the Windows default): forking a process that already loaded OpenCV
    # or PyTorch threads can deadlock on Linux.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=max_workers, mp_context=ctx,
                             initializer=speed.worker_init, initargs=(str(settings.data_dir),)) as pool:
        running: dict = {}
        while True:
            if more_coming:
                add_new()
            limit = workers or gate.allowed()
            while queue and len(running) < limit:
                r, start, end = queue.pop(0)
                began.setdefault(r["id"], time.perf_counter())
                if fast:
                    fut = pool.submit(analyse_range, r["id"], r["path"], start, min(end, r["duration"] or end),
                                      r["width"] or 0, r["height"] or 0, str(settings.keyframe_dir),
                                      settings.scene_fps, decoder)
                else:
                    fut = pool.submit(analyse_video, r["id"], r["path"], r["duration"], str(settings.keyframe_dir))
                running[fut] = r
            if not running:
                if more_coming and more_coming():
                    time.sleep(1.0)
                    continue
                if more_coming:
                    add_new()  # the last download may have landed just now
                    if queue:
                        continue
                break
            finished, _ = wait(list(running), timeout=2, return_when=FIRST_COMPLETED)
            for fut in finished:
                asset = running.pop(fut)
                aid = asset["id"]
                try:
                    results[aid] += fut.result()
                except Exception as e:  # noqa: BLE001
                    log.warning("scene detection failed for %s: %s", asset["source_id"], e)
                    failed.add(aid)
                    conn.execute("UPDATE assets SET status='failed', error=? WHERE id=?", (str(e)[:500], aid))
                    conn.commit()
                parts[aid] -= 1
                if parts[aid]:
                    continue
                done_assets += 1
                if aid not in failed:
                    found = sorted(results[aid], key=lambda f: f["start"])
                    save_scenes(conn, aid, found)
                    total += len(found)
                    timing.record(conn, "scenes", time.perf_counter() - began[aid], 1, asset=aid,
                                  video_seconds=round(asset["duration"] or 0), height=asset["height"],
                                  detector=settings.scene_detector, decoder=decoder)
                    log.info("[%d/%d] %s: %d scenes", done_assets, len(seen), asset["source_id"], len(found))
                progress.report(done_assets, len(seen), "cut scenes")
    return total
