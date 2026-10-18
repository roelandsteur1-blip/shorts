"""Assemble a 1080x1920 Short with ffmpeg: crop to 9:16, gentle motion, animated text (ASS/libass),
background music (shorts/music.py) at -14 LUFS. Original footage audio is never used."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from . import db, looks, media
from .config import Channel, Settings
from .music import MUSIC_EXT  # noqa: F401 - imported from here by the dashboard and 'check'

W, H, FPS = 1080, 1920, 30
END_FADE = 0.3  # seconds: end sharply on the payoff, no long music fade (research/shorts-studies.md)
FONTS_DIR = looks.FONTS_DIR


# ---------- subtitles ----------

def _fontsdir(work: Path) -> str:
    """FONTS_DIR for ffmpeg's ass filter: relative to work, or absolute (colon escaped) when the
    render folder is on another drive."""
    try:
        return os.path.relpath(FONTS_DIR, work).replace("\\", "/")
    except ValueError:
        return FONTS_DIR.as_posix().replace(":", "\\:")


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(text: str, highlight: str, primary: str) -> str:
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    return re.sub(r"\*(.+?)\*", lambda m: f"{{\\c{highlight}}}{m.group(1)}{{\\c{primary}}}", text).replace("*", "")


def build_ass(blocks: list[dict], style: dict, title: str | None = None) -> str:
    """style: the channel's [style]; its look (shorts/looks.py) sets font and outline or box.
    title: the screen title, shown at the top per the channel's title_bar (always / hook / off)."""
    bar = looks.title_bar(style)
    title_size = looks.title_size(title, style) if title and bar != "off" else 0
    style = looks.resolve(style)
    font = style.get("font", "Arial Black")
    size = style.get("font_size", 88)
    hook_size = style.get("hook_font_size", size + 16)
    primary = style.get("primary_color", "&H00FFFFFF")
    highlight = style["highlight_color"]
    outline = style.get("outline", 6)
    margin_v = style.get("margin_v", 760)
    ml, mr = looks.MARGIN_L, looks.MARGIN_R
    # Outline: black edge + soft shadow. Box: a dark see-through box behind the text (BorderStyle 3
    # fills the box with the outline colour; the outline width is the padding).
    border, edge, shadow, hook_edge = (3, "&H50000000", 0, outline) if style["box"] else (1, "&H00000000", 3, outline + 1)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Body,{font},{size},{primary},{primary},{edge},&H80000000,-1,0,0,0,100,100,0,0,{border},{outline},{shadow},2,{ml},{mr},{margin_v},1
Style: Hook,{font},{hook_size},{primary},{primary},{edge},&H80000000,-1,0,0,0,100,100,0,0,{border},{hook_edge},{shadow},2,{ml},{mr},{margin_v},1
Style: Title,{font},{title_size or size},{primary},{primary},{edge},&H80000000,-1,0,0,0,100,100,0,0,{border},{max(outline - 2, 3) if style["box"] else outline},{shadow},8,{ml},{mr},{looks.TITLE_TOP},1

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
    if title_size:
        until = t if bar == "always" else blocks[0]["seconds"]
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(until - 0.04)},Title,,0,0,0,,"
                     rf"{{\fad(0,{0 if bar == 'always' else 120})}}{_ass_text(title, highlight, primary)}")
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
        # Scale the source once to the output width; the blurred background is made from a
        # quarter-size copy of that (blur hides the detail anyway) and scaled up: far cheaper.
        return (
            f"{src},scale={W}:-2:flags=bicubic,split=2[bg{idx}][fgs{idx}];"
            f"[bg{idx}]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
            f"boxblur=6:2,eq=brightness=-0.08,scale={W}:{H}[bgb{idx}];"
            f"[bgb{idx}][fgs{idx}]overlay=(W-w)/2:(H-h)/2,trim=end_frame={frames},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{idx}]"
        )
    # Cover 1080x1920 with 8% headroom, then pan slowly around crop_x.
    dur = max(seconds, 0.1)
    pan = drift * W * 0.06
    x = f"max(0,min(iw-ow,{crop_x:.3f}*iw-ow/2+({pan:.1f})*t/{dur:.3f}))"
    return (
        f"{src},scale={int(W * 1.08)}:{int(H * 1.08)}:force_original_aspect_ratio=increase:"
        f"force_divisible_by=2:flags=bicubic,crop={W}:{H}:x='{x}':y='(ih-oh)/2',"
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


def pick_music(conn: sqlite3.Connection, settings: Settings, short_id: int,
               channel: Channel | None = None) -> tuple[Path, float] | None:
    """(track, where to start in it), chosen by shorts/music.py: mood, least used, not the previous one."""
    from . import music
    t = music.pick(conn, settings, short_id, channel)
    return (Path(t["path"]), t["start"] or 0.0) if t else None


def work_dir(settings: Settings, channel: Channel, short_id: int) -> Path:
    return settings.render_dir / f"{channel.id}-{short_id:05d}"


def render_short(conn: sqlite3.Connection, settings: Settings, channel: Channel, short_id: int) -> Path:
    from . import speed, timing, voice
    voice.prepare(conn, settings, channel, short_id)  # no-op unless voiced and the text changed
    looks.ensure_fonts({looks.resolve(channel.style).get("font")})
    lim = speed.limits(settings.data_dir)
    enc = media.video_encoder_args(settings.encoder, settings.x264_preset, lim.x264_threads)
    work, args, total, music = build_render(conn, settings, channel, short_id, enc)
    out = work / "short.mp4"
    with timing.timed(conn, "render", short=short_id, seconds_of_video=round(total, 1), encoder=enc[1]):
        media.run(args, cwd=work)
    os.replace(work / "out.mp4", out)

    meta = metadata(conn, channel, short_id)
    (work / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    # A re-render of an approved Short stays approved; everything else goes (back) to review.
    current = conn.execute("SELECT status FROM shorts WHERE id=?", (short_id,)).fetchone()[0]
    db.set_short(conn, short_id, status="approved" if current == "approved" else "rendered", video_path=str(out), duration=total,
                 music=str(music) if music else None, description=meta["description"])
    return out


def build_render(conn: sqlite3.Connection, settings: Settings, channel: Channel, short_id: int,
                 encoder_args: list[str], out_name: str = "out.mp4") -> tuple[Path, list[str], float, Path | None]:
    """The ffmpeg arguments for one Short (run them inside the returned work folder: ffmpeg's ass
    filter and Windows drive letters don't mix). Writes subs.ass; touches nothing else, so
    'bench' can render with other settings. Returns (work folder, args, seconds, music)."""
    media.require_ffmpeg()
    blocks = [dict(r) for r in conn.execute(
        """SELECT b.idx, b.text, b.seconds, b.scene_id, sc.start, sc.end, sc.crop_x, a.path, a.width, a.height
             FROM blocks b JOIN scenes sc ON sc.id = b.scene_id JOIN assets a ON a.id = sc.asset_id
            WHERE b.short_id = ? ORDER BY b.idx""", (short_id,))]
    if not blocks:
        raise ValueError(f"short {short_id} has no blocks with footage")

    work = work_dir(settings, channel, short_id)
    work.mkdir(parents=True, exist_ok=True)
    title = conn.execute("SELECT screen_title FROM shorts WHERE id=?", (short_id,)).fetchone()[0]
    (work / "subs.ass").write_text(build_ass(blocks, channel.style, title), encoding="utf-8")

    inputs, filters, offsets = [], [], {}
    # 4K sources: the GPU's video decoder does the heavy decoding, frames come back for the CPU
    # filters. 1080p is cheap on the CPU and keeps GPU memory free for parallel renders.
    gpu_decode = (settings.render_gpu_decode and settings.encoder != "libx264"
                  and any((b["height"] or 0) > 1080 for b in blocks) and media.nvdec_works())
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
        if gpu_decode and (b["height"] or 0) > 1080:
            inputs += ["-hwaccel", "cuda"]
        inputs += ["-ss", f"{start:.3f}", "-t", f"{min(b['seconds'] + 0.2, avail):.3f}", "-i", b["path"]]
        mode = choose_mode(b["width"] or W, b["height"] or H, b["crop_x"] if b["crop_x"] is not None else 0.5)
        drift = 1 if i % 2 == 0 else -1
        filters.append(_segment_filter(i, b["seconds"], b["crop_x"] if b["crop_x"] is not None else 0.5,
                                       b["width"] or W, b["height"] or H, mode, drift, hold))

    n = len(blocks)
    total = sum(b["seconds"] for b in blocks)
    concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]"
    fontsdir = _fontsdir(work) if FONTS_DIR.exists() else None
    subs = "ass=subs.ass" + (f":fontsdir='{fontsdir}'" if fontsdir else "")
    filters += [concat, f"[vcat]{subs}[vout]"]

    row = conn.execute("SELECT voice FROM shorts WHERE id=?", (short_id,)).fetchone()
    voice_wav = work / "voice.wav"
    voiced = bool(row and row["voice"]) and voice_wav.exists()
    stereo = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    idx = n
    picked = pick_music(conn, settings, short_id, channel)
    music = picked[0] if picked else None
    if picked:
        # Looped, so a track shorter than the Short (or a late start) never runs out. Under a
        # voice it sits well below the speech and is ducked further while someone talks.
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        filters.append(
            f"[{idx}:a]atrim=start={picked[1]:.2f}:duration={total:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:d=0.3,afade=t=out:st={max(total - END_FADE, 0):.3f}:d={END_FADE},"
            f"loudnorm=I={-24 if voiced else -14}:TP=-1.5:LRA=11,{stereo}[mus]"
        )
        idx += 1
    if voiced:
        inputs += ["-i", str(voice_wav)]
        filters.append(f"[{idx}:a]{stereo},apad,atrim=0:{total:.3f},loudnorm=I=-14:TP=-1.5:LRA=11,{stereo}[voc]")
        if picked:
            filters += ["[voc]asplit=2[vkey][vmix]",
                        "[mus][vkey]sidechaincompress=threshold=0.04:ratio=6:attack=15:release=400[duck]",
                        "[duck][vmix]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.9,"
                        f"loudnorm=I=-14:TP=-1.5:LRA=11,{stereo}[aout]"]
        else:
            filters.append("[voc]anull[aout]")
    elif picked:
        filters.append("[mus]anull[aout]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        filters.append(f"[{idx}:a]anull[aout]")

    args = inputs + [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
        *encoder_args,
        "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", out_name,
    ]
    return work, args, total, music


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


def hashtags(channel: Channel) -> list[str]:
    """YouTube shows three hashtags above the title: two about the topic, then #shorts."""
    tags = [h for h in dict.fromkeys(channel.hashtags) if h.lower() != "#shorts"]
    return tags[:2] + ["#shorts"]


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
    parts += ["", " ".join(hashtags(channel))]
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
