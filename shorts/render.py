"""Assemble a 1080x1920 Short with ffmpeg: crop to 9:16 around the subject (shorts/framing.py),
slow zooms and punch-ins (plan_motion), animated text (ASS/libass),
background music (shorts/music.py) at -14 LUFS. Original footage audio is never used."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from . import db, looks, media, tags as taglayers
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


def _spoken(token: str, highlight: str) -> str:
    """The word being said, in the highlight colour (a *key word* already is)."""
    return token if "*" in token else f"*{token}*"


def build_ass(blocks: list[dict], style: dict, title: str | None = None,
              word_times: list | None = None) -> str:
    """style: the channel's [style]; its look (shorts/looks.py) sets font and outline or box.
    title: the screen title, shown at the top per the channel's title_bar (always / hook / off).
    word_times: per block, when each word is spoken (voice.word_timings): the word being said is
    highlighted too. The caption stays the whole block; only the colour of one word changes."""
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
        kind = "Hook" if i == 0 else "Body"
        pop = r"{\fad(90,70)\fscx82\fscy82\t(0,140,\fscx100\fscy100)}"
        words = (word_times or [None] * len(blocks))[i] if word_times and i < len(word_times) else None
        tokens = b["text"].split()
        if words and len(words) == len(tokens) and len(tokens) > 1:
            # One event per spoken word, all with the same layout: only the colour of the word moves.
            # The first carries the pop-in, the last the fade-out.
            marks = [start] + [start + w[0] for w in words[1:]] + [end - 0.04]
            for k in range(len(tokens)):
                a, z = marks[k], max(marks[k + 1], marks[k] + 0.04)
                if a >= end - 0.04:
                    break
                text = " ".join(_spoken(tok, highlight) if j == k else tok for j, tok in enumerate(tokens))
                tag = (r"{\fad(90,0)\fscx82\fscy82\t(0,140,\fscx100\fscy100)}" if k == 0 else
                       r"{\fad(0,70)}" if k == len(tokens) - 1 else "")
                lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(min(z, end - 0.04))},{kind},,0,0,0,,"
                             f"{tag}{_ass_text(text, highlight, primary)}")
        else:
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end - 0.04)},{kind},,0,0,0,,"
                         f"{pop}{_ass_text(b['text'], highlight, primary)}")
        t = end
    if title_size:
        until = t if bar == "always" else blocks[0]["seconds"]
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(until - 0.04)},Title,,0,0,0,,"
                     rf"{{\fad(0,{0 if bar == 'always' else 120})}}{_ass_text(title, highlight, primary)}")
    return head + "\n".join(lines) + "\n"


# ---------- video ----------

# Motion per block (shorts/render.py plan_motion): zoom factors relative to the 9:16 window.
KEN_BURNS = 0.06   # slow zoom in or out on an ordinary block
HOOK_PUSH = 0.10   # the hook pushes in a little faster
PUNCH = 0.12       # a quick punch-in on a block with a highlighted word...
PUNCH_SECONDS = 0.2
PUNCH_GAP = 2      # ...at most every other block, and at most MAX_PUNCHES per Short
MAX_PUNCHES = 3
PAYOFF_PUSH = 0.07  # the last block pushes in slowly toward the payoff
MAX_ZOOM = 1.25


def plan_motion(blocks: list[dict]) -> list[dict]:
    """{'kind', 'z0', 'z1'} per block: the hook pushes in, highlighted words get a punch-in (not
    two in a row, at most three), the last block pushes in toward the payoff, the rest slowly
    zoom in or out in turn. A block that continues the previous block's shot carries on from its
    zoom, so the cut between them doesn't jump."""
    plans: list[dict] = []
    punches, last_punch = 0, -PUNCH_GAP
    for i, b in enumerate(blocks):
        prev = plans[-1] if plans else None
        same = prev is not None and b.get("scene_id") == blocks[i - 1].get("scene_id")
        z0 = prev["z1"] if same else 1.0
        if i == 0:
            kind, z1 = "push", 1.0 + HOOK_PUSH
        elif "*" in (b.get("text") or "") and i - last_punch >= PUNCH_GAP and punches < MAX_PUNCHES:
            kind, z1 = "punch", min(z0 + PUNCH, MAX_ZOOM)
            punches, last_punch = punches + 1, i
        elif i == len(blocks) - 1:
            kind, z1 = "push", min(z0 + PAYOFF_PUSH, MAX_ZOOM)
        else:
            if same:  # keep going the way the shot was going; turn around at the limits
                zoom_in = prev["kind"] == "in"
                if zoom_in and z0 + KEN_BURNS > MAX_ZOOM:
                    zoom_in = False
                if not zoom_in and z0 - KEN_BURNS < 1.0:
                    zoom_in = True
            else:
                zoom_in = i % 2 == 1
                if not zoom_in:
                    z0 = 1.0 + KEN_BURNS
            kind = "in" if zoom_in else "out"
            z1 = z0 + KEN_BURNS if zoom_in else z0 - KEN_BURNS
        if kind == "punch" and z1 - z0 < 0.02:  # no room left to punch in: zoom back out instead
            kind, z1 = "out", max(1.0, z0 - KEN_BURNS)
        plans.append({"kind": kind, "z0": round(z0, 4), "z1": round(z1, 4)})
    return plans


def _zoom_expr(plan: dict, frames: int) -> str:
    """zoompan's zoom for output frame `on`."""
    z0, z1 = plan["z0"], plan["z1"]
    u = f"min(on/{max(frames - 1, 1)},1)"
    if plan["kind"] == "punch":
        p = max(1, round(PUNCH_SECONDS * FPS))
        jump = (z1 - z0) * 0.85  # most of it in the first fifth of a second, the rest slowly
        return f"{z0:.4f}+{jump:.4f}*(1-pow(1-min(on/{p},1),3))+{z1 - z0 - jump:.4f}*{u}"
    if plan["kind"] == "push":
        return f"{z0:.4f}+{z1 - z0:.4f}*(1-pow(1-{u},2))"
    return f"{z0:.4f}+{z1 - z0:.4f}*(0.5-0.5*cos(PI*{u}))"


OLD_FILM_SOURCES = {"prelinger", "internet_archive", "europeana", "openbeelden", "loc"}
OLD_FILM_CLEAN = "hqdn3d=3:2:4:4,unsharp=5:5:0.5"  # grain and flicker down, then a little bite back


STILL_MOTION = 0.012  # mean frame change (0-1) below which a shot counts as nearly still...
SPEED_UP = 1.2        # ...and plays this much faster, unless there are people in it


def speed_for(frame: dict, seconds: float, avail: float) -> float:
    """Nearly still footage without faces (a rocket on the pad, a landscape) plays a little faster
    so it feels alive; anything with people in it stays at normal speed. Only when the scene has
    enough footage left for it."""
    motion = frame.get("motion")
    if motion is None or motion >= STILL_MOTION or frame.get("faces"):
        return 1.0
    return SPEED_UP if avail >= seconds * SPEED_UP + 0.2 else 1.0


def old_film(source: str | None, height: int) -> bool:
    """Archive film at SD size: worth a denoise before it is scaled up (cheap at that size)."""
    return (source or "") in OLD_FILM_SOURCES and 0 < (height or 0) <= 576


def _segment_filter(idx: int, seconds: float, frame: dict, width: int, height: int,
                    mode: str, plan: dict | None = None, hold: float = 0.0, clean: bool = False,
                    speed: float = 1.0) -> str:
    """Scale/crop one input to 1080x1920. frame: shorts/framing.py's subject path (where the 9:16
    window sits at the start and end of the block); plan: plan_motion's zoom.
    hold > 0: the scene is shorter than the block, so its last frame is held instead of running
    on past the cut into the next shot of the source video."""
    frames = max(1, round(seconds * FPS))
    src = (f"[{idx}:v]" + (f"setpts=PTS/{speed:.3f}," if speed != 1.0 else "") + f"fps={FPS}"
           + (f",tpad=stop_mode=clone:stop_duration={hold:.3f}" if hold > 0 else ""))
    if clean:
        src += "," + OLD_FILM_CLEAN
    tail = f"trim=end_frame={frames},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{idx}]"
    if mode == "blur":
        # Scale the source once to the output width; the blurred background is made from a
        # quarter-size copy of that (blur hides the detail anyway) and scaled up: far cheaper.
        return (
            f"{src},scale={W}:-2:flags=bicubic,split=2[bg{idx}][fgs{idx}];"
            f"[bg{idx}]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
            f"boxblur=6:2,eq=brightness=-0.08,scale={W}:{H}[bgb{idx}];"
            f"[bgb{idx}][fgs{idx}]overlay=(W-w)/2:(H-h)/2,{tail}"
        )
    from .framing import window_fraction
    fw, fh = window_fraction(width, height)
    # The window's left/top edge (fractions of the source) at the start and end of the block.
    l0, l1 = frame["x0"] - fw / 2, frame["x1"] - fw / 2
    t0, t1 = frame["y0"] - fh / 2, frame["y1"] - fh / 2
    # 1) Cut the band the window moves through out of the source, so only that part is scaled.
    bl, br = max(0.0, min(l0, l1)), min(1.0, max(l0, l1) + fw)
    bt, bb = max(0.0, min(t0, t1)), min(1.0, max(t0, t1) + fh)
    bw = max(W, round((br - bl) / fw * W / 2) * 2)
    bh = max(H, round((bb - bt) / fh * H / 2) * 2)
    band = (f"crop=w='iw*{br - bl:.5f}':h='ih*{bb - bt:.5f}':x='iw*{bl:.5f}':y='ih*{bt:.5f}',"
            f"scale={bw}:{bh}:flags=bicubic")
    # 2) Pan through it at output resolution (smooth), easing in and out.
    u = f"(0.5-0.5*cos(PI*min(n/{max(frames - 1, 1)},1)))"
    sx, sy = W / fw, H / fh  # output pixels per source-frame fraction
    x = f"max(0,min(iw-ow,{(l0 - bl) * sx:.2f}+{(l1 - l0) * sx:.2f}*{u}))"
    y = f"max(0,min(ih-oh,{(t0 - bt) * sy:.2f}+{(t1 - t0) * sy:.2f}*{u}))"
    out = f"{src},{band},crop={W}:{H}:x='{x}':y='{y}'"
    # 3) Zoom around the subject (fy: how high it sits in the window).
    if plan and (plan["z0"] != 1.0 or plan["z1"] != 1.0):
        fy = min(max(frame.get("fy", 0.5), 0.3), 0.7)
        out += (f",zoompan=z='{_zoom_expr(plan, frames)}':x='0.5*(iw-iw/zoom)':y='{fy:.3f}*(ih-ih/zoom)'"
                f":d=1:s={W}x{H}:fps={FPS}")
    return f"{out},{tail}"


def choose_mode(width: int, height: int) -> str:
    """Crop to 9:16 (the window follows the subject, shorts/framing.py); a blurred background only
    for very wide pictures (panoramas, cinemascope), where a crop would keep too little of them."""
    return "blur" if width / max(height, 1) > 2.2 else "crop"


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

    meta = metadata(conn, channel, short_id, settings)
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
        """SELECT b.idx, b.text, b.seconds, b.scene_id, sc.start, sc.end, sc.crop_x, a.path, a.width, a.height, a.source
             FROM blocks b JOIN scenes sc ON sc.id = b.scene_id JOIN assets a ON a.id = sc.asset_id
            WHERE b.short_id = ? ORDER BY b.idx""", (short_id,))]
    if not blocks:
        raise ValueError(f"short {short_id} has no blocks with footage")

    work = work_dir(settings, channel, short_id)
    work.mkdir(parents=True, exist_ok=True)
    title = conn.execute("SELECT screen_title FROM shorts WHERE id=?", (short_id,)).fetchone()[0]
    picked = pick_music(conn, settings, short_id, channel)
    music = picked[0] if picked else None
    if picked and settings.render_beat_cuts:
        # Cuts on the music's beat (moved by at most 0.15 s; only in this render, the stored block
        # lengths stay as they are). Captions move with the cuts.
        from . import music as musiclib
        snapped = musiclib.snap_to_beats([b["seconds"] for b in blocks],
                                         musiclib.beats(music, picked[1], sum(b["seconds"] for b in blocks)))
        for b, secs in zip(blocks, snapped):
            b["seconds"] = secs
    from . import voice as voicelib
    voiced_row = conn.execute("SELECT voice FROM shorts WHERE id=?", (short_id,)).fetchone()
    words = voicelib.word_timings(work) if voiced_row and voiced_row["voice"] and settings.render_word_highlight else None
    (work / "subs.ass").write_text(build_ass(blocks, channel.style, title, words), encoding="utf-8")

    inputs, filters, offsets = [], [], {}
    # 4K sources: the GPU's video decoder does the heavy decoding, frames come back for the CPU
    # filters. 1080p is cheap on the CPU and keeps GPU memory free for parallel renders.
    gpu_decode = (settings.render_gpu_decode and settings.encoder != "libx264"
                  and any((b["height"] or 0) > 1080 for b in blocks) and media.nvdec_works())
    for b in blocks:
        # Consecutive blocks on the same scene continue where the previous one stopped.
        off = offsets.get(b["scene_id"], 0.0)
        start = b["start"] + off
        if start + b["seconds"] > b["end"] + 0.05:
            start = max(b["start"], b["end"] - b["seconds"])
        offsets[b["scene_id"]] = off + b["seconds"]
        b["src_start"] = start
        # Never read past the scene's end: that is a cut to another shot (or a title card).
        b["avail"] = max(b["end"] - start, 0.1)

    plans = plan_motion(blocks) if settings.render_zoom else [None] * len(blocks)
    frames = _subject_paths(work, blocks)
    for i, b in enumerate(blocks):
        if i and b["scene_id"] == blocks[i - 1]["scene_id"]:
            # Same shot as the block before: start where its window ended (no jump on the cut).
            frames[i] = {**frames[i], "x0": frames[i - 1]["x1"], "y0": frames[i - 1]["y1"]}
        avail = b["avail"]
        # Not on a shot that the next or previous block continues: that one starts where this
        # block would have stopped at normal speed.
        shared = (i and blocks[i - 1]["scene_id"] == b["scene_id"]) or \
            (i + 1 < len(blocks) and blocks[i + 1]["scene_id"] == b["scene_id"])
        speed = speed_for(frames[i], b["seconds"], avail) if settings.render_speed_ramps and not shared else 1.0
        need = b["seconds"] * speed + 0.2
        hold = (need - avail) / speed if avail < need else 0.0
        if gpu_decode and (b["height"] or 0) > 1080:
            inputs += ["-hwaccel", "cuda"]
        inputs += ["-ss", f"{b['src_start']:.3f}", "-t", f"{min(need, avail):.3f}", "-i", b["path"]]
        mode = choose_mode(b["width"] or W, b["height"] or H)
        filters.append(_segment_filter(i, b["seconds"], frames[i], b["width"] or W, b["height"] or H,
                                       mode, plans[i], hold,
                                       settings.render_grade and old_film(b["source"], b["height"] or 0), speed))

    n = len(blocks)
    total = sum(b["seconds"] for b in blocks)
    concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]"
    fontsdir = _fontsdir(work) if FONTS_DIR.exists() else None
    subs = "ass=subs.ass" + (f":fontsdir='{fontsdir}'" if fontsdir else "")
    graded = looks.grade(channel.style, channel.id) if settings.render_grade else None
    filters += [concat, f"[vcat]{graded + ',' if graded else ''}{subs}[vout]"]

    row = conn.execute("SELECT voice FROM shorts WHERE id=?", (short_id,)).fetchone()
    voice_wav = work / "voice.wav"
    voiced = bool(row and row["voice"]) and voice_wav.exists()
    stereo = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    idx = n
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

    if settings.render_sfx:
        sfx = sound_effects(blocks, plans)
        if sfx:
            # The last filter wrote [aout]: rename it and mix the effects in underneath.
            filters[-1] = filters[-1][: filters[-1].rindex("[aout]")] + "[amain]"
            filters += sfx + ["[amain][sfx]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[aout]"]

    args = inputs + [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
        *encoder_args,
        "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", out_name,
    ]
    return work, args, total, music


SFX_VOLUME = 0.12  # whoosh and hit sit well under the music and voice


def sound_effects(blocks: list[dict], plans: list[dict | None]) -> list[str]:
    """Filters making [sfx]: a soft whoosh into every cut to another shot and a low hit under each
    punch-in, both made by ffmpeg itself (filtered noise, a decaying low sine): no files, no licence.
    [] when there is nothing to put a sound on."""
    whooshes, hits, t = [], [], 0.0
    for i, b in enumerate(blocks):
        if i and b["scene_id"] != blocks[i - 1]["scene_id"]:
            whooshes.append(max(t - 0.22, 0.0))  # the whoosh peaks on the cut
        if plans[i] and plans[i]["kind"] == "punch":
            hits.append(t)
        t += b["seconds"]
    if not whooshes and not hits:
        return []
    # One source per sound: an asplit of one source into delayed copies stalls ffmpeg 6's amix.
    out, labels = [], []
    for k, at in enumerate(whooshes):
        out.append(f"anoisesrc=d=0.35:c=pink:r=48000:a=0.9,highpass=f=400,lowpass=f=4500,"
                   f"afade=t=in:d=0.22:curve=exp,afade=t=out:st=0.22:d=0.13,adelay={int(at * 1000)}:all=1[wd{k}]")
        labels.append(f"[wd{k}]")
    for k, at in enumerate(hits):
        out.append(f"sine=f=58:d=0.45:r=48000,volume=2.5,afade=t=out:st=0.02:d=0.43:curve=exp,"
                   f"adelay={int(at * 1000)}:all=1[hd{k}]")
        labels.append(f"[hd{k}]")
    total = sum(b["seconds"] for b in blocks)
    out.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
               f"volume={SFX_VOLUME},apad=whole_dur={total:.3f},atrim=0:{total:.3f},aresample=48000,"
               "aformat=sample_fmts=fltp:channel_layouts=stereo[sfx]")
    return out


def _subject_paths(work: Path, blocks: list[dict]) -> list[dict]:
    """shorts/framing.py's subject path for every block (a few at once: each is one small ffmpeg
    read), cached in the render folder so a re-render or 'bench' doesn't read them again."""
    from concurrent.futures import ThreadPoolExecutor
    from . import framing
    cache = framing.load_cache(work)

    def one(b: dict) -> dict:
        return framing.subject_path(b["path"], b["src_start"], min(b["seconds"], b["avail"]),
                                    b["width"] or W, b["height"] or H,
                                    b["crop_x"] if b["crop_x"] is not None else 0.5, cache)

    with ThreadPoolExecutor(max_workers=max(1, min(4, len(blocks), os.cpu_count() or 1))) as pool:
        out = list(pool.map(one, blocks))
    framing.save_cache(work, cache)
    return out


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


def all_tags(post: list[str], channel: Channel, settings=None) -> list[str]:
    """The 9 tags of a Short: 3 post, 3 niche, 3 broad (shorts/tags.py)."""
    p, n, b = taglayers.layers(post, channel, settings)
    return p + n + b


def metadata(conn: sqlite3.Connection, channel: Channel, short_id: int, settings=None) -> dict:
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
    nine = all_tags(json.loads(s["tags"] or "[]"), channel, settings)
    parts += ["", " ".join(dict.fromkeys(taglayers.hashtag(t) for t in nine))]
    title = clean_text(s["title"]).strip() or clean_text(s["topic"] or s["hook"] or "").replace("*", "").strip()
    return {
        "title": title[:100],
        "description": clean_text("\n".join(parts).strip())[:4900],
        "tags": [clean_text(t) for t in nine if clean_text(t).strip()],
        "categoryId": "28",  # Science & Technology
        "containsSyntheticMedia": bool(s["synthetic"]),
        "selfDeclaredMadeForKids": False,
    }
