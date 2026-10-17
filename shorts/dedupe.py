"""Keep the library free of duplicates.

The same NASA id is never downloaded twice (UNIQUE in the database). But NASA often publishes the
same footage again under a new id (4K re-release, B-roll package, re-edit). Three checks catch that:
1. before download: same title once resolution/format tags are stripped;
2. after download: a visual fingerprint of the whole video close to one already in the library;
3. per scene: a scene that looks the same as a scene from another video is not used twice.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

log = logging.getLogger(__name__)

FP_STEP = 1.0           # seconds between sampled frames
FP_MAX_FRAMES = 300     # cap for very long videos
HASH_MATCH_BITS = 5     # two frames "match" when their 64-bit dHashes differ in <= this many bits
VIDEO_MATCH_SHARE = 0.6 # a video is a duplicate when this share of its frames match the other video
SCENE_SIMILARITY = 0.95 # CLIP cosine above which two scenes from different videos are the same shot

_TAGS = re.compile(r"\b(\d{3,4}p|4k|8k|uhd|hd|hdr|60fps|30fps|m\d{3,5}|v\d|version \d|final|mp4|mov)\b")


def normalise_title(title: str | None) -> str:
    t = (title or "").lower().replace("_", " ")
    t = _TAGS.sub(" ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def same_title(conn: sqlite3.Connection, title: str | None, exclude_id: int | None = None,
               date: str | None = None, ignore_new_after: int | None = None) -> int | None:
    """Id of a usable asset with the same normalised title and the same date, if any. NASA reuses
    titles for different events (e.g. each "SLS Full-Scale Booster Test"), so the date must match."""
    key = normalise_title(title)
    if len(key) < 12:  # very short titles ("Launch") say too little to call a duplicate
        return None
    day = (date or "")[:10]
    for r in conn.execute("""SELECT id, title, date_created, status FROM assets
                              WHERE status NOT IN ('failed', 'duplicate', 'rejected')"""):
        if ignore_new_after is not None and r["status"] == "new" and r["id"] > ignore_new_after:
            continue  # a later copy that is not downloaded yet: it will be the duplicate, not this one
        if (r["id"] != exclude_id and normalise_title(r["title"]) == key
                and (r["date_created"] or "")[:10] == day):
            return r["id"]
    return None


def _dhash(frame: np.ndarray) -> int:
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def fingerprint(path: str) -> bytes | None:
    """One frame hash about every FP_STEP seconds (8 bytes each), at most FP_MAX_FRAMES. Near-black
    or flat frames are skipped: they look alike in every video and would cause false matches.

    One ffmpeg pass that decodes only keyframes (no seeking, tiny frames): many times faster than
    seeking through the file. Footage with few keyframes gets a full (still small) decode instead,
    so a fingerprint always has enough distinct frames."""
    duration = _duration(path)
    if not duration:
        return None
    n = int(min(FP_MAX_FRAMES, max(4, duration / FP_STEP)))
    rate = n / duration
    hashes = _hash_frames(path, rate, keyframes_only=True)
    if hashes is not None and len(set(hashes)) < max(3, len(hashes) // 3):
        hashes = _hash_frames(path, rate, keyframes_only=False)
    if not hashes or len(hashes) < 3:
        return None
    return b"".join(h.to_bytes(8, "big") for h in hashes)


def _duration(path: str) -> float:
    try:
        from .media import probe
        return probe(path)["duration"]
    except Exception:  # noqa: BLE001 - unreadable file: no fingerprint
        return 0.0


def _hash_frames(path: str, rate: float, keyframes_only: bool) -> list[int] | None:
    import subprocess
    w, h = 64, 36  # point-sampled (neighbor): averaging would turn fine detail into a flat grey
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "2"]
    if keyframes_only:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-i", path, "-an", "-sn", "-vf", f"fps={rate:.6f},scale={w}:{h}:flags=neighbor,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        log.debug("fingerprint failed for %s: %s", path, proc.stderr[-300:])
        return None
    frames = np.frombuffer(proc.stdout, dtype=np.uint8)
    frames = frames[: len(frames) // (w * h) * (w * h)].reshape(-1, h, w)
    out = []
    for gray in frames:
        # Dark night shots and starfields hash alike across unrelated videos: skip them.
        if gray.mean() < 25 or gray.std() < 20:
            continue
        out.append(_dhash_gray(gray))
    return out


def _dhash_gray(gray: np.ndarray) -> int:
    g = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def _hashes(fp: bytes) -> np.ndarray:
    return np.frombuffer(fp, dtype=">u8").astype(np.uint64)


def _popcount(x: np.ndarray) -> np.ndarray:
    as_bytes = x.astype(">u8").view(np.uint8).reshape(*x.shape, 8)
    return np.unpackbits(as_bytes, axis=-1).sum(axis=-1)


def _longest_increasing(seq: list[int]) -> int:
    import bisect
    tails: list[int] = []
    for x in seq:
        i = bisect.bisect_right(tails, x)  # non-decreasing: a still shot repeats one frame hash
        tails[i:i + 1] = [x]
    return len(tails)


def similar(fp_a: bytes, fp_b: bytes) -> bool:
    """True when most frames of the shorter video appear in the other one *in the same order*.
    Order tolerates trims (a copy that starts later still lines up) but rules out unrelated
    videos whose frames only happen to resemble each other here and there."""
    a, b = _hashes(fp_a), _hashes(fp_b)
    if len(a) > len(b):
        a, b = b, a
    dist = _popcount(np.bitwise_xor(a[:, None], b[None, :]))
    best = dist.argmin(axis=1)
    ok = dist[np.arange(len(a)), best] <= HASH_MATCH_BITS
    in_order = _longest_increasing([int(j) for j in best[ok]])
    return bool(in_order / len(a) >= VIDEO_MATCH_SHARE)


def same_footage(conn: sqlite3.Connection, fp: bytes, exclude_id: int | None = None) -> int | None:
    for r in conn.execute("""SELECT id, fingerprint FROM assets
                              WHERE fingerprint IS NOT NULL AND status NOT IN ('failed', 'duplicate', 'rejected')"""):
        if r["id"] != exclude_id and similar(fp, r["fingerprint"]):
            return r["id"]
    return None


def mark_duplicate(conn: sqlite3.Connection, asset_id: int, original_id: int, why: str) -> None:
    conn.execute("UPDATE assets SET status='duplicate', error=? WHERE id=?",
                 (f"{why} as asset {original_id}", asset_id))
    conn.commit()


def reject_duplicate_scenes(conn: sqlite3.Connection, threshold: float = SCENE_SIMILARITY,
                            chunk: int = 1024) -> int:
    """Reject scenes that look the same as a scene from another (older) video. Scenes inside one
    video are left alone: consecutive chunks of a long shot are meant to look alike."""
    rows = conn.execute("""SELECT id, asset_id, embedding FROM scenes
                            WHERE embedding IS NOT NULL AND rejected = 0 ORDER BY asset_id, id""").fetchall()
    if len(rows) < 2:
        return 0
    mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    assets = np.array([r["asset_id"] for r in rows])
    keep = np.ones(len(rows), dtype=bool)
    idx = np.arange(len(rows))
    # Similarities a chunk of rows at a time: a full N x N matrix would need ~10 GB at 50k scenes.
    for c in range(0, len(rows), chunk):
        sims = mat[c:c + chunk] @ mat.T
        for i in range(c, min(c + chunk, len(rows))):
            if not keep[i]:
                continue
            dup = (sims[i - c] >= threshold) & (assets != assets[i]) & (idx > i) & keep
            keep[dup] = False
    rejected = [(int(rows[i]["id"]),) for i in np.where(~keep)[0]]
    conn.executemany("UPDATE scenes SET rejected=1, reject_reason='duplicate of another video' WHERE id=?",
                     rejected)
    conn.commit()
    return len(rejected)


def dedupe_library(conn: sqlite3.Connection) -> dict:
    """Backfill fingerprints for footage downloaded before this check existed and mark later
    copies (the oldest one stays). Removes the duplicate files' scenes from use."""
    out = {"fingerprinted": 0, "duplicate_videos": 0, "duplicate_scenes": 0}
    todo = conn.execute("""SELECT id, path FROM assets WHERE fingerprint IS NULL AND path IS NOT NULL
                            AND status NOT IN ('failed', 'duplicate', 'rejected') ORDER BY id""").fetchall()
    # OpenCV decodes outside the GIL, so threads use all cores; only this thread writes the DB.
    workers = max(1, min(len(todo), (os.cpu_count() or 2) - 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r, fp in zip(todo, pool.map(lambda r: fingerprint(r["path"]), todo)):
            if fp:
                conn.execute("UPDATE assets SET fingerprint=? WHERE id=?", (fp, r["id"]))
                out["fingerprinted"] += 1
    conn.commit()
    # Shortest first: when a short B-roll cut is contained in a longer video, the longer one stays.
    rows = conn.execute("""SELECT id, title, date_created, fingerprint, duration FROM assets
                            WHERE status NOT IN ('failed', 'duplicate', 'rejected')
                            ORDER BY COALESCE(duration, 0), id""").fetchall()
    for r in rows:
        if conn.execute("SELECT status FROM assets WHERE id=?", (r["id"],)).fetchone()[0] == "duplicate":
            continue
        other = same_title(conn, r["title"], r["id"], r["date_created"])
        if other is None and r["fingerprint"]:
            other = same_footage(conn, r["fingerprint"], r["id"])
        if other is not None:
            mark_duplicate(conn, r["id"], other, "same footage")
            conn.execute("UPDATE scenes SET rejected=1, reject_reason='duplicate video' WHERE asset_id=?", (r["id"],))
            out["duplicate_videos"] += 1
    conn.commit()
    out["duplicate_scenes"] = reject_duplicate_scenes(conn)
    return out
