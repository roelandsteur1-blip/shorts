"""Thin wrappers around ffmpeg/ffprobe."""
from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise FFmpegError(f"{tool} not found on PATH (install with: winget install Gyan.FFmpeg)")


def run(args: list[str], quiet: bool = True) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if quiet:
        cmd += ["-loglevel", "error"]
    proc = subprocess.run(cmd + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.strip()[-2000:] or f"ffmpeg failed: {args}")
    return proc


def probe(path: Path | str) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.strip() or f"ffprobe failed on {path}")
    data = json.loads(proc.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or (video or {}).get("duration") or 0)
    return {
        "duration": duration,
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
        "has_audio": audio is not None,
        "codec": video.get("codec_name") if video else None,
    }


def strip_audio(src: Path, dst: Path) -> None:
    """Copy the video stream only. Original audio often holds third-party music."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["-i", str(src), "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "copy",
             "-movflags", "+faststart", str(dst)])
    except FFmpegError:
        # Some sources (ProRes/odd containers) can't be stream-copied into mp4: re-encode.
        run(["-i", str(src), "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(dst)])


@lru_cache(maxsize=1)
def _nvenc_works() -> bool:
    try:
        run(["-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc",
             "-f", "null", "-"])
        return True
    except FFmpegError:
        return False


def video_encoder_args(preference: str = "auto") -> list[str]:
    """Encoder flags for the final 1080x1920 render."""
    if preference in ("auto", "h264_nvenc") and _nvenc_works():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19",
                "-b:v", "12M", "-maxrate", "16M", "-bufsize", "24M",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-profile:v", "high", "-pix_fmt", "yuv420p"]
