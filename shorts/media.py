"""Thin wrappers around ffmpeg/ffprobe."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise FFmpegError(f"{tool} not found on PATH (install with: winget install Gyan.FFmpeg)")


def run(args: list[str], quiet: bool = True, cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if quiet:
        cmd += ["-loglevel", "error"]
    proc = subprocess.run(cmd + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=cwd)
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


NVENC_ARGS = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19",
              "-b:v", "12M", "-maxrate", "16M", "-bufsize", "24M",
              "-spatial-aq", "1", "-temporal-aq", "1", "-bf", "3", "-rc-lookahead", "20",
              "-profile:v", "high", "-pix_fmt", "yuv420p"]


@lru_cache(maxsize=1)
def _nvenc_works() -> bool:
    """Try a tiny GPU encode with the real settings. On failure, log ffmpeg's reason once: it names
    the NVIDIA driver version it needs."""
    try:
        run(["-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2", *NVENC_ARGS, "-f", "null", "-"])
        return True
    except FFmpegError as e:
        lines = str(e).splitlines()
        why = [ln for ln in lines if "driver" in ln.lower() or "api" in ln.lower()] or lines[:3]
        log.warning("NVENC (GPU encoding) not available, using the CPU instead. ffmpeg says: %s",
                    " | ".join(why))
        return False


@lru_cache(maxsize=1)
def nvdec_works() -> bool:
    """GPU decoding (-hwaccel cuda) on a tiny H.264 clip; also needs a recent NVIDIA driver."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "probe.mp4"
        try:
            run(["-f", "lavfi", "-i", "testsrc2=s=320x180:d=0.5", "-c:v", "libx264", "-preset",
                 "ultrafast", "-pix_fmt", "yuv420p", str(clip)])
            # Plain -hwaccel cuda silently falls back to the CPU; frames that must stay on the GPU
            # (hwaccel_output_format cuda) make a broken driver fail loudly instead.
            run(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(clip),
                 "-vf", "hwdownload,format=nv12", "-f", "null", "-"])
            ok = True
        except FFmpegError:
            ok = False
    if not ok:
        log.info("NVDEC (GPU decoding) not available: decoding on the CPU")
    return ok


def video_encoder_args(preference: str = "auto", x264_preset: str = "veryfast",
                       threads: int = 0) -> list[str]:
    """Encoder flags for the final 1080x1920 render. YouTube re-encodes every upload, so a fast
    x264 preset with a slightly lower CRF looks the same after upload and takes a fraction of the
    time. threads: x264 threads (0 = all cores; Quiet mode passes fewer)."""
    if preference in ("auto", "h264_nvenc") and _nvenc_works():
        return list(NVENC_ARGS)
    crf = "18" if x264_preset in ("medium", "slow", "slower") else "17"
    return ["-c:v", "libx264", "-preset", x264_preset, "-crf", crf,
            "-profile:v", "high", "-pix_fmt", "yuv420p"] + (["-threads", str(threads)] if threads else [])
