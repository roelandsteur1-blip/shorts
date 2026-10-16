"""Assemble a 1080x1920 Short with ffmpeg: crop to 9:16, gentle motion, animated text (ASS/libass),
optional royalty-free music at -14 LUFS. Original footage audio is never used."""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from . import db, media
from .config import Channel, Settings

W, H, FPS = 1080, 1920, 30
FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"


# ---------- subtitles ----------

def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(text: str, highlight: str, primary: str) -> str:
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    return re.sub(r"\*(.+?)\*", lambda m: f"{{\\c{highlight}}}{m.group(1)}{{\\c{primary}}}", text).replace("*", "")


def build_ass(blocks: list[dict], style: dict) -> str:
    font = style.get("font", "Arial Black")
    size = style.get("font_size", 88)
    hook_size = style.get("hook_font_size", size + 16)
    primary = style.get("primary_color", "&H00FFFFFF")
    highlight = style.get("highlight_color", "&H0000D7FF")
    outline = style.get("outline", 6)
    margin_v = style.get("margin_v", 760)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Body,{font},{size},{primary},{primary},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},3,2,90,90,{margin_v},1
Style: Hook,{font},{hook_size},{primary},{primary},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline + 1},3,2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines, t = [], 0.0
    for i, b in enumerate(blocks):
        start, end = t, t + b["seconds"]
        pop = r"{\fad(90,70)\fscx82\fscy82\t(0,140,\fscx100\fscy100)}"
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end - 0.04)},{'Hook' if i == 0 else 'Body'},,0,0,0,,"
            f"{pop}{_ass_text(b['text'], highlight, primary)}"
        )
        t = end
    return head + "\n".join(lines) + "\n"


# ---------- video ----------

def _segment_filter(idx: int, seconds: float, crop_x: float, width: int, height: int,
                    mode: str, drift: float, hold: float = 0.0) -> str:
    """Scale/crop one input to 1080x1920 with a slow pan or zoom so the frame never feels static.
    hold > 0: the scene is shorter than the block, so its last frame is held instead of running
    on past the cut into the next shot of the source video."""
    frames = max(1, round(seconds * FPS))
    src = f"[{idx}:v]fps={FPS}" + (f",tpad=stop_mode=clone:stop_duration={hold:.3f}" if hold > 0 else "")
    if mode == "blur":
        return (
            f"{src},split=2[bg{idx}][fg{idx}];"
            f"[bg{idx}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,eq=brightness=-0.08[bgb{idx}];"
            f"[fg{idx}]scale={W}:-2[fgs{idx}];"
            f"[bgb{idx}][fgs{idx}]overlay=(W-w)/2:(H-h)/2,trim=end_frame={frames},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{idx}]"
        )
    # Cover 1080x1920 with 8% headroom, then pan slowly around crop_x.
    dur = max(seconds, 0.1)
    pan = drift * W * 0.06
    x = f"max(0,min(iw-ow,{crop_x:.3f}*iw-ow/2+({pan:.1f})*t/{dur:.3f}))"
    return (
        f"{src},scale={int(W * 1.08)}:{int(H * 1.08)}:force_original_aspect_ratio=increase:"
        f"force_divisible_by=2:flags=lanczos,crop={W}:{H}:x='{x}':y='(ih-oh)/2',"
        f"trim=end_frame={frames},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{idx}]"
    )


def choose_mode(width: int, height: int, crop_x: float) -> str:
    """Centre crop by default; blurred background only when the subject would be cut off."""
    aspect = width / max(height, 1)
    if aspect < 1.0:
        return "crop"  # already portrait
    if crop_x < 0.18 or crop_x > 0.82:
        return "blur"
    return "crop"


MUSIC_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac")


def pick_music(settings: Settings, short_id: int, channel: Channel | None = None) -> Path | None:
    """Each channel has its own folder (music/<music_folder>, default music/<channel id>),
    so a history or war channel never gets space music."""
    def tracks_in(folder: Path) -> list[Path]:
        return sorted(p for p in folder.glob("*") if p.suffix.lower() in MUSIC_EXT)

    tracks = tracks_in(settings.music_dir / (channel.music_folder or channel.id)) if channel else []
    tracks = tracks or tracks_in(settings.music_dir)  # loose tracks: fallback for every channel
    if not tracks:
        return None
    return random.Random(short_id).choice(tracks)


def render_short(conn: sqlite3.Connection, settings: Settings, channel: Channel, short_id: int) -> Path:
    media.require_ffmpeg()
    blocks = [dict(r) for r in conn.execute(
        """SELECT b.idx, b.text, b.seconds, b.scene_id, sc.start, sc.end, sc.crop_x, a.path, a.width, a.height
             FROM blocks b JOIN scenes sc ON sc.id = b.scene_id JOIN assets a ON a.id = sc.asset_id
            WHERE b.short_id = ? ORDER BY b.idx""", (short_id,))]
    if not blocks:
        raise ValueError(f"short {short_id} has no blocks with footage")

    work = settings.render_dir / f"{channel.id}-{short_id:05d}"
    work.mkdir(parents=True, exist_ok=True)
    (work / "subs.ass").write_text(build_ass(blocks, channel.style), encoding="utf-8")

    inputs, filters, offsets = [], [], {}
    for i, b in enumerate(blocks):
        # Consecutive blocks on the same scene continue where the previous one stopped.
        off = offsets.get(b["scene_id"], 0.0)
        start = b["start"] + off
        if start + b["seconds"] > b["end"] + 0.05:
            start = max(b["start"], b["end"] - b["seconds"])
        offsets[b["scene_id"]] = off + b["seconds"]
        # Never read past the scene's end: that is a cut to another shot (or a title card).
        avail = max(b["end"] - start, 0.1)
        hold = b["seconds"] + 0.2 - avail if avail < b["seconds"] + 0.2 else 0.0
        inputs += ["-ss", f"{start:.3f}", "-t", f"{min(b['seconds'] + 0.2, avail):.3f}", "-i", b["path"]]
        mode = choose_mode(b["width"] or W, b["height"] or H, b["crop_x"] if b["crop_x"] is not None else 0.5)
        drift = 1 if i % 2 == 0 else -1
        filters.append(_segment_filter(i, b["seconds"], b["crop_x"] if b["crop_x"] is not None else 0.5,
                                       b["width"] or W, b["height"] or H, mode, drift, hold))

    n = len(blocks)
    total = sum(b["seconds"] for b in blocks)
    concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]"
    fontsdir = os.path.relpath(FONTS_DIR, work).replace("\\", "/") if FONTS_DIR.exists() else None
    subs = "ass=subs.ass" + (f":fontsdir='{fontsdir}'" if fontsdir else "")
    filters += [concat, f"[vcat]{subs}[vout]"]

    music = pick_music(settings, short_id, channel)
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        filters.append(
            f"[{n}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:d=0.3,afade=t=out:st={max(total - 1.2, 0):.3f}:d=1.2,"
            f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]"
        )
    else:
        inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        filters.append(f"[{n}:a]anull[aout]")

    out = work / "short.mp4"
    args = inputs + [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
        *media.video_encoder_args(settings.encoder),
        "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", "out.mp4",
    ]
    _run_in(work, args)
    os.replace(work / "out.mp4", out)

    meta = metadata(conn, channel, short_id)
    (work / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    # A re-render of an approved Short stays approved; everything else goes (back) to review.
    current = conn.execute("SELECT status FROM shorts WHERE id=?", (short_id,)).fetchone()[0]
    db.set_short(conn, short_id, status="approved" if current == "approved" else "rendered", video_path=str(out), duration=total,
                 music=str(music) if music else None, description=meta["description"])
    return out


def _run_in(cwd: Path, args: list[str]) -> None:
    """ffmpeg's ass filter and Windows drive letters don't mix; run inside the work folder."""
    old = os.getcwd()
    os.chdir(cwd)
    try:
        media.run(args)
    finally:
        os.chdir(old)


# Links in a Shorts description are not clickable, so sources are named, not linked.
SOURCE_NAMES = [
    ("jpl.nasa.gov", "NASA JPL"), ("svs.gsfc.nasa.gov", "NASA Scientific Visualization Studio"),
    ("science.nasa.gov", "NASA Science"), ("images.nasa.gov", "NASA Image and Video Library"),
    ("nasa.gov", "NASA"), ("esa.int", "ESA"), ("esahubble.org", "ESA/Hubble"),
    ("esawebb.org", "ESA/Webb"), ("noaa.gov", "NOAA"),
]


def source_name(url: str) -> str:
    host = urlsplit(url).hostname or url
    host = host.lower().removeprefix("www.")
    for domain, name in SOURCE_NAMES:
        if host == domain or host.endswith("." + domain):
            return name
    return host


def clean_text(text: str) -> str:
    """YouTube rejects titles and descriptions containing < or >."""
    return (text or "").replace("<", "").replace(">", "")


def footage_credit(asset) -> str:
    """Credit for one clip, names only (no links): the license's required attribution when there
    is one, else the archive and license, e.g. 'NASA (public domain)'."""
    if asset["attribution"]:
        return asset["attribution"]
    if asset["source"] == "nasa":
        return "NASA (public domain)"
    from .sources import label
    lic = asset["license"] or ""
    return f"{label(asset['source'])} ({lic})" if lic and lic != "unknown" else label(asset["source"])


def metadata(conn: sqlite3.Connection, channel: Channel, short_id: int) -> dict:
    s = conn.execute("SELECT * FROM shorts WHERE id=?", (short_id,)).fetchone()
    assets = conn.execute(
        """SELECT DISTINCT a.title, a.source, a.license, a.attribution FROM blocks b
             JOIN scenes sc ON sc.id = b.scene_id JOIN assets a ON a.id = sc.asset_id
            WHERE b.short_id = ? ORDER BY b.idx""", (short_id,)).fetchall()
    sources = sorted({source_name(f["source_url"]) for f in conn.execute(
        "SELECT source_url FROM facts WHERE short_id=? AND source_url IS NOT NULL", (short_id,))})
    base = (s["description"] or "").split("\n\nFootage:")[0].strip()
    credit_lines = []
    for a in assets:
        credit_lines.append(f"- {footage_credit(a)}: {a['title']}")
    parts = [base, "", "Footage:", *dict.fromkeys(credit_lines)]
    if sources:
        parts += ["", "Sources: " + ", ".join(sources)]
    if any(a["source"] == "nasa" for a in assets):
        parts += ["", "NASA does not endorse this channel."]
    parts += ["", " ".join(channel.hashtags)]
    tags = json.loads(s["tags"] or "[]")
    title = clean_text(s["title"]).strip() or clean_text(s["topic"] or s["hook"] or "").replace("*", "").strip()
    return {
        "title": title[:100],
        "description": clean_text("\n".join(parts).strip())[:4900],
        "tags": [clean_text(t) for t in tags if clean_text(t).strip()],
        "categoryId": "28",  # Science & Technology
        "containsSyntheticMedia": bool(s["synthetic"]),
        "selfDeclaredMadeForKids": False,
    }
