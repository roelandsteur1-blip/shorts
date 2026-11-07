"""Light review copies of rendered Shorts: 720x1280 at a low bitrate (about a quarter of the
1080p file), so a Short starts at once on the phone over Tailscale. Made in the background by the
dashboard, one at a time and at low priority; until a copy is ready the full file is served. The
file name carries the render's mtime, so a re-render gets a new copy."""
from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

_queue: "queue.Queue[tuple[Settings, int, str]]" = queue.Queue()
_queued: set[str] = set()
_lock = threading.Lock()
_worker: threading.Thread | None = None


def folder(settings: Settings) -> Path:
    return settings.data_dir / "previews"


def path_for(settings: Settings, sid: int, src: str) -> Path | None:
    try:
        stamp = os.stat(src).st_mtime_ns
    except OSError:
        return None
    return folder(settings) / f"{sid}-{stamp}.mp4"


def ready(settings: Settings, sid: int, src: str | None) -> Path | None:
    """The light copy when it exists."""
    p = path_for(settings, sid, src) if src else None
    return p if p and p.exists() else None


def make(settings: Settings, sid: int, src: str) -> Path | None:
    """Encode the light copy now (blocking). Old copies of this Short are removed."""
    out = path_for(settings, sid, src)
    if out is None or out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "2", "-i", src,
           "-vf", "scale=720:-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
           "-maxrate", "1500k", "-bufsize", "3000k", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(tmp)]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "15", *cmd]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        log.warning("preview for #%s failed: %s", sid, (p.stderr or "")[-300:])
        return None
    tmp.replace(out)
    for old in out.parent.glob(f"{sid}-*.mp4"):
        if old != out:
            old.unlink(missing_ok=True)
    return out


def queue_for(settings: Settings, shorts) -> None:
    """Make light copies of these Shorts (rows with id and video_path) in the background."""
    global _worker
    with _lock:
        for s in shorts:
            src = s["video_path"]
            if not src or ready(settings, s["id"], src):
                continue
            key = f"{s['id']}:{src}"
            if key not in _queued:
                _queued.add(key)
                _queue.put((settings, s["id"], src))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="previews", daemon=True)
            _worker.start()


def _work() -> None:
    global _worker
    while True:
        try:
            settings, sid, src = _queue.get(timeout=60)
        except queue.Empty:
            with _lock:
                if _queue.empty():
                    _worker = None
                    return
            continue
        try:
            make(settings, sid, src)
        except Exception:  # noqa: BLE001 - a broken file must not stop the others
            log.exception("preview for #%s", sid)
        finally:
            with _lock:
                _queued.discard(f"{sid}:{src}")


def wait_idle(timeout: float = 120.0) -> None:
    """Tests: until the queue is empty."""
    import time
    end = time.monotonic() + timeout
    while (_queued or not _queue.empty()) and time.monotonic() < end:
        time.sleep(0.05)
