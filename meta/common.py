"""Shared pieces for the meta maker's formats: opening the footage library read-only, finding
candidate clips with keyframes, and rendering a Short from timed segments (clip in a band in the
middle, a full-screen text overlay on top, music under it)."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from .textcard import H, W, black_bars, find_clips, log, pick_track

BAND_Y = 700  # top of the video band: room for the title and lists above it


def open_library():
    """(settings, conn, llm) the way `python -m shorts` sets them up on the server."""
    from shorts import db, llm
    from shorts.config import load_settings
    from shorts.server import load_env
    from . import config_dir
    settings = load_settings(config_dir())
    load_env(settings)
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")
    return settings, db.connect(settings.db_path), llm


def candidates(conn, settings, queries: list[str], used: set[int], need: float, per_query: int = 3) -> dict:
    """{'C1': clip, ...} for every query; each clip has a keyframe, a file and enough length."""
    out = {}
    for q in queries:
        for c in find_clips(conn, settings, [q], used | {x["id"] for x in out.values()}, need, per_query):
            out[f"C{len(out) + 1}"] = c
    return out


def candidate_lines(cands: dict) -> tuple[str, list[bytes]]:
    lines = [f"{k}: archive title \"{c['title']}\" (searched for: {c['query']})" for k, c in cands.items()]
    return "\n".join(lines), [Path(c["keyframe"]).read_bytes() for c in cands.values()]


VERIFY = """Each image below is ONE video clip: three frames from its start, middle and end, side
by side. Look only at the frames (ignore any title you may know). Say ok only if ALL THREE frames
clearly and unmistakably show the named subject as the main thing on screen, in one continuous
shot. It is a no when: a frame shows something else (a cut to another scene, a title card, a
crowd, a presenter or diagram taking over), or the subject is a different building, animal, plane
or object that merely looks similar. Say yes only if you would bet on it.

{lines}

Return JSON only: {{"checks": [{{"label": "A", "ok": true}}]}}"""


def verify(llm, settings, items: list[tuple[str, bytes, str]]) -> set[str]:
    """Blind second look: [(label, keyframe JPEG, claimed name)] -> labels whose image really
    shows that subject."""
    if not items:
        return set()
    lines = "\n".join(f"Image {lab}: {name}" for lab, _, name in items)
    data = llm.ask_json(VERIFY.format(lines=lines), settings.claude_cmd, retries=1,
                        images=[img for _, img, _ in items])
    return {c.get("label") for c in data.get("checks", []) if c.get("ok") is True}


def clip_strip(clip: dict, seconds: float) -> bytes:
    """One JPEG with frames from the start, middle and end of the part of the clip we will show,
    so the check sees cuts and title cards the scene table missed (round 3/4)."""
    import io
    frames = []
    for t in (0.3, seconds / 2, max(0.5, seconds - 0.3)):
        p = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{clip['start'] + t:.2f}", "-i", clip["path"],
                            "-frames:v", "1", "-vf", "scale=480:-2", "-f", "image2", "-c:v", "mjpeg", "-"],
                           capture_output=True)
        if p.stdout:
            frames.append(Image.open(io.BytesIO(p.stdout)).convert("RGB"))
    if not frames:
        return Path(clip["keyframe"]).read_bytes()
    h = max(f.height for f in frames)
    strip = Image.new("RGB", (sum(f.width for f in frames) + 20 * (len(frames) - 1), h), "white")
    x = 0
    for f in frames:
        strip.paste(f, (x, 0))
        x += f.width + 20
    buf = io.BytesIO()
    strip.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def band_height(clip: dict, crop: str | None) -> int:
    cw, ch = clip["width"], clip["height"]
    if crop:
        cw, ch = map(int, crop.split("=")[1].split(":")[:2])
    return min(int(W * ch / cw), 760) // 2 * 2


def render_segments(segments: list[dict], track, out: Path) -> float:
    """segments: [{clip, offset (s from the scene start), seconds, overlay (RGBA W x H image)}].
    Each segment shows the clip in the band with the overlay on top; then all are joined and the
    music is laid under. Returns the length in seconds."""
    from shorts import media
    crops: dict[int, str | None] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        parts = []
        for i, seg in enumerate(segments):
            clip = seg["clip"]
            if clip["id"] not in crops:
                crops[clip["id"]] = black_bars(clip["path"], clip["start"])
            crop = crops[clip["id"]]
            vh = band_height(clip, crop)
            png = tmp / f"o{i}.png"
            seg["overlay"].save(png)
            part = tmp / f"p{i}.mp4"
            fc = (f"[1:v]{crop + ',' if crop else ''}scale={W}:{vh},setsar=1,fps=30[v];"
                  f"[0:v][v]overlay=0:{BAND_Y + (760 - vh) // 2}[b];[b][2:v]overlay=0:0,format=yuv420p[out]")
            media.run(["-f", "lavfi", "-t", f"{seg['seconds']:.2f}", "-i", f"color=black:s={W}x{H}:r=30",
                       "-ss", f"{clip['start'] + seg.get('offset', 0):.2f}", "-t", f"{seg['seconds']:.2f}",
                       "-i", clip["path"], "-loop", "1", "-t", f"{seg['seconds']:.2f}", "-i", str(png),
                       "-filter_complex", fc, "-map", "[out]", "-an", "-r", "30", "-c:v", "libx264",
                       "-preset", "medium", "-crf", "20", str(part)])
            parts.append(part)
        listing = tmp / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
        total = sum(s["seconds"] for s in segments)
        args = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        if track:
            args += ["-ss", f"{track['start'] or 0:.2f}", "-t", f"{total:.2f}", "-i", track["path"]]
        else:
            args += ["-f", "lavfi", "-t", f"{total:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        args += ["-filter_complex", "[1:a]volume=0.55,aresample=48000[a]", "-map", "0:v", "-map", "[a]",
                 "-t", f"{total:.2f}", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                 "-movflags", "+faststart", str(out)]
        media.run(args)
    return total


def blank() -> Image.Image:
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


__all__ = ["BAND_Y", "open_library", "candidates", "candidate_lines", "render_segments", "blank",
           "pick_track", "log", "W", "H", "clip_strip"]
