"""Text-card Shorts (meta maker, phase 1): one surprising fact as a post-style card on the top half,
one archive clip below, 6-15 seconds, hard cut so it loops. Research: research/youtube-meta.md.

Samples only: renders MP4s into ~/meta-out/textcard/, never uploads and never writes to the
`shorts` database (it only reads footage, scenes and music from it).

    python -m meta.textcard --niche space -n 3
    python -m meta.textcard --all -n 2          # every niche below
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import textwrap
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
OUT = Path.home() / "meta-out" / "textcard"
FONT_DIR = Path.home() / "meta-out" / "fonts"
FONT_URLS = {
    "bold": "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf",
    "regular": "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Medium.ttf",
}

# What wins in each niche right now (research/youtube-meta.md, "Hot sub-topics per niche").
NICHES = {
    "space": ("Space", "scale what-ifs (the Sun next to monster stars), sounds of planets, unexplained "
              "signals, how long it takes to reach places, the 1859 solar storm, black holes"),
    "history": ("History", "rare photos and the story behind them, legendary weapons and inventions, one "
                "person's extraordinary war story, strange laws and jobs of the past"),
    "body": ("Human Body", "what happens inside your body when you do or eat something, X vs Y inside "
             "your body, what happens if you swallow or leave something in, how organs work"),
    "warplane": ("Warplanes", "why a famous aircraft can do something surprising, how pilots survived old "
                 "planes, ejection and escape, record speeds and heights, weird inventions"),
    "ocean": ("Ocean", "deep-sea creatures, the rarest or most extreme things in the sea, why an animal "
              "behaves a certain way, deadliest sea animals"),
    "planet": ("Wild Planet", "animal behaviour that seems impossible, deadliest animals, how an animal "
               "survives, record-breaking animals"),
    "weather": ("Weather", "the most extreme weather ever recorded, what if scenarios, how tornadoes and "
                "lightning really work"),
    "earthquake": ("Earth", "the biggest disasters ever recorded, what if scenarios, strange natural "
                   "phenomena"),
    "retrotech": ("Retro Tech", "what was inside old devices, how a vintage machine worked, inventions "
                  "that were ahead of their time"),
    "oldcity": ("Old Cities", "why a city exists where it does, abandoned ancient cities, how a city "
                "looked long ago"),
}

PROMPT = """You write very short "did you know" posts for a YouTube Shorts channel about {topic}.
Each Short shows the post as a card on screen for about 10 seconds, with a matching archive video
below it. These cards get millions of views when the fact makes people stop and rewatch.

What wins in this niche right now: {hot}.

Write {n} posts. Rules:
- One concrete, surprising, TRUE fact per post about one specific thing. Only well-documented facts,
  no rumours, no exaggeration. Give your source.
- 18 to 38 words. Plain words a 10-year-old understands. No hashtags, no emojis except at most one
  at the end.
- The first words must create curiosity (e.g. "In 1977, a signal from deep space lasted 72 seconds...").
- End on the most surprising part, so the viewer wants to watch again.
- Each post needs an archive video that fits. Our library is mostly NASA, US government and
  1920s-1970s public-domain film. Give two short visual search queries of what can be SEEN: a
  specific one ("B-17 bomber in flight") and a broader one that old archive film surely has
  ("WWII bomber plane"). Prefer subjects such footage can show.
- Different subjects for every post. Avoid: {avoid}.

Return JSON only:
{{"posts": [{{"text": "...", "highlight": ["1-3 key words from the text"],
"visual": "specific query", "visual_broad": "broader query",
"title": "YouTube title, a question or claim, max 60 characters",
"source": "where this fact is documented"}}]}}"""


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    path = FONT_DIR / Path(FONT_URLS[kind]).name
    if not path.exists():
        try:
            urllib.request.urlretrieve(FONT_URLS[kind], path)
        except OSError:
            fallback = Path("/usr/share/fonts/truetype/dejavu") / (
                "DejaVuSans-Bold.ttf" if kind == "bold" else "DejaVuSans.ttf")
            return ImageFont.truetype(str(fallback), size)
    return ImageFont.truetype(str(path), size)


def wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if f.getlength(test) <= width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


def card_image(text: str, highlight: list[str], name: str, handle: str, colour: tuple) -> Image.Image:
    """The post card, dark theme, full width; height fits the text."""
    pad, size = 60, 50 if len(text) < 170 else 44
    body, bold = font("regular", size), font("bold", 40)
    lines = wrap(text, body, W - 2 * pad)
    line_h = int(size * 1.38)
    head = 150
    h = head + len(lines) * line_h + 50
    im = Image.new("RGB", (W, h), (0, 0, 0))
    d = ImageDraw.Draw(im)
    # avatar: coloured circle with the initial
    d.ellipse((pad, 30, pad + 96, 126), fill=colour)
    ini = font("bold", 50)
    d.text((pad + 48, 78), name[:1], font=ini, fill="white", anchor="mm")
    d.text((pad + 120, 40), name, font=bold, fill="white")
    d.text((pad + 120, 90), handle, font=font("regular", 32), fill=(113, 118, 123))
    keys = {w.lower().strip(".,!?\"'") for h_ in highlight for w in h_.split()}
    y = head
    for ln in lines:
        x = pad
        for w in ln.split():
            hot = w.lower().strip(".,!?\"'") in keys
            d.text((x, y), w, font=body, fill=(255, 215, 0) if hot else (231, 233, 234))
            x += body.getlength(w + " ")
        y += line_h
    return im


LOOP_MIN = 4.0  # a shot shorter than the card's time is looped; it must be at least this long


def seconds_for(text: str) -> float:
    return max(6.0, min(15.0, len(text.split()) / 2.7 + 1.5))


CHECK = """You check short "did you know" posts before they are published on a {topic} channel.
For each post you get its text and up to {k} candidate video frames, labelled like P2-3 (post 2,
candidate 3), in the order listed below.

For every post:
1. Fact check. Is every claim, name, date and number correct? If something is wrong but a small
   edit makes the post correct, give the corrected full text in "fix". If unsure, facts_ok=false.
2. Footage. Pick the candidate frame that clearly shows the post's subject, or exactly the kind of
   thing it names (any WWII bomber is fine for a story about a WWII bomber; a ship is NOT fine for
   a story about wind). A viewer must never feel the video is unrelated. If none fits, null.
3. Duplicate: true if the post has the same subject as an earlier post in this list.

Posts:
{posts}

Return JSON only: {{"checks": [{{"post": 1, "facts_ok": true, "fix": "", "clip": "P1-2", "duplicate": false}}]}}"""


def has_cut(path: str, start: float, seconds: float, threshold: float = 0.25) -> bool:
    """True when the video cuts to another shot between start and start+seconds. The scene table's
    boundaries miss some cuts (round 3: a crowd before the Eiffel Tower, a puppet after a jellyfish)."""
    import subprocess
    p = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{start + 0.1:.2f}", "-t", f"{seconds:.2f}", "-i", path,
                        "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    return "pts_time" in p.stderr


def find_clips(conn, settings, queries: list[str], used: set[int], need: float, per_query: int = 2,
               min_len: float | None = None) -> list[dict]:
    """Up to per_query scenes for each query whose video file exists, is landscape and has one
    unbroken shot of `need` seconds (or at least `min_len` seconds when the caller loops short
    shots)."""
    from shorts import embed
    out, seen = [], set(used)
    for query in filter(None, queries):
        got = 0
        for sid, score in embed.search(conn, settings, query, k=40, exclude=seen):
            r = conn.execute("""SELECT sc.id, sc.start, sc.end, sc.keyframe, a.path, a.duration, a.title,
                                       a.source, a.width, a.height
                                  FROM scenes sc JOIN assets a ON a.id = sc.asset_id WHERE sc.id=?""",
                             (sid,)).fetchone()
            if not r or not r["path"] or not Path(r["path"]).exists():
                continue
            if not r["keyframe"] or not Path(r["keyframe"]).exists():
                continue
            # The whole clip must stay inside its own shot, or it cuts to an unrelated scene.
            shot = r["end"] - r["start"]
            if shot < (min_len or need) or (r["width"] or 0) < (r["height"] or 1):
                continue
            if has_cut(r["path"], r["start"], min(shot, need)):
                continue
            seen.add(sid)
            out.append(dict(r) | {"score": round(score, 3), "query": query})
            got += 1
            if got >= per_query:
                break
    return out


def black_bars(path: str, start: float) -> str | None:
    """ffmpeg crop filter that removes black bars around old film, or None when there are none."""
    import subprocess
    p = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{start + 0.5:.2f}", "-t", "2", "-i", path,
                        "-vf", "cropdetect=limit=24:round=2", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", p.stderr)
    if not found:
        return None
    w, h, x, y = map(int, found[-1])
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        fw, fh = map(int, probe.stdout.strip().split(",")[:2])
    except ValueError:
        return None
    if w * h > 0.96 * fw * fh or w < fw * 0.5 or h < fh * 0.5:
        return None
    return f"crop={w}:{h}:{x}:{y}"


def pick_track(conn):
    rows = conn.execute("SELECT path, start FROM tracks WHERE status='ok'").fetchall()
    rows = [r for r in rows if r["path"] and Path(r["path"]).exists()]
    return random.choice(rows) if rows else None


def render(card: Image.Image, clip: dict, track, seconds: float, out: Path) -> None:
    from shorts import media
    crop = black_bars(clip["path"], clip["start"])
    cw, ch = (clip["width"], clip["height"])
    if crop:
        cw, ch = map(int, crop.split("=")[1].split(":")[:2])
    vid_h = min(int(W * ch / cw), 1000) // 2 * 2
    total = card.height + 24 + vid_h
    top = max(240, (H - total) // 2 - 60)  # a little above centre: bottom of the screen has the UI
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    canvas.paste(card, (0, top))
    png = out.with_suffix(".png")
    canvas.save(png)
    shot = clip["end"] - clip["start"]
    if shot < seconds:  # loop the one shot instead of running into the next one
        loop = out.with_suffix(".shot.mp4")
        media.run(["-ss", f"{clip['start']:.2f}", "-t", f"{shot - 0.1:.2f}", "-i", clip["path"], "-an",
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(loop)])
        args = ["-loop", "1", "-i", str(png), "-stream_loop", "-1", "-t", f"{seconds:.2f}", "-i", str(loop)]
    else:
        loop = None
        args = ["-loop", "1", "-i", str(png), "-ss", f"{clip['start']:.2f}", "-t", f"{seconds:.2f}",
                "-i", clip["path"]]
    if track:
        args += ["-ss", f"{track['start'] or 0:.2f}", "-t", f"{seconds:.2f}", "-i", track["path"]]
    else:
        args += ["-f", "lavfi", "-t", f"{seconds:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
    fc = (f"[1:v]{crop + ',' if crop else ''}scale={W}:{vid_h},setsar=1,fps=30[v];"
          f"[0:v][v]overlay=0:{top + card.height + 24}:shortest=1,format=yuv420p[out];"
          f"[2:a]volume=0.55,aresample=48000[a]")
    args += ["-filter_complex", fc, "-map", "[out]", "-map", "[a]", "-t", f"{seconds:.2f}",
             "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    media.run(args)
    png.unlink(missing_ok=True)
    if loop:
        loop.unlink(missing_ok=True)


def make(niche: str, n: int, seed_avoid: list[str]) -> list[dict]:
    from shorts import db, llm
    from shorts.config import load_settings
    from shorts.server import load_env
    from . import config_dir
    settings = load_settings(config_dir())
    load_env(settings)  # CLAUDE_CODE_OAUTH_TOKEN on the server, as `python -m shorts` does
    local_bin = str(Path.home() / ".local" / "bin")  # where the installer puts claude
    if local_bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")
    conn = db.connect(settings.db_path)
    topic, hot = NICHES[niche]
    out_dir = OUT / niche
    out_dir.mkdir(parents=True, exist_ok=True)
    done_file = OUT / "made.json"
    made = json.loads(done_file.read_text(encoding="utf-8")) if done_file.exists() else []
    avoid = [m["title"] for m in made if m["niche"] == niche] + seed_avoid
    log(f"[{niche}] asking Claude for {n + 4} posts")
    data = llm.ask_json(PROMPT.format(topic=topic, hot=hot, n=n + 4,
                                      avoid="; ".join(avoid[-40:]) or "nothing yet"),
                        settings.claude_cmd, retries=1)
    used = {m["scene_id"] for m in made}
    colour = random.Random(niche).choice([(232, 65, 24), (29, 155, 240), (0, 186, 124),
                                          (120, 86, 255), (249, 24, 128)])
    posts = data.get("posts", [])
    lines, images, cands = [], [], {}
    for i, post in enumerate(posts, 1):
        post["text"] = re.sub(r"\s+", " ", post["text"]).strip()
        clips = find_clips(conn, settings, [post.get("visual"), post.get("visual_broad")], used,
                           seconds_for(post["text"]), min_len=LOOP_MIN)
        labels = []
        for j, c in enumerate(clips, 1):
            cands[f"P{i}-{j}"] = c
            labels.append(f"P{i}-{j}")
            images.append(Path(c["keyframe"]).read_bytes())
        lines.append(f"Post {i}: {post['text']}\n  Frames: {', '.join(labels) or 'none'}")
    log(f"[{niche}] {len(posts)} posts, {len(cands)} candidate clips: asking Claude to check facts and footage")
    checks = llm.ask_json(CHECK.format(topic=topic, k=4, posts="\n".join(lines)), settings.claude_cmd,
                          retries=1, images=images).get("checks", [])
    by_post = {int(c.get("post", 0)): c for c in checks}
    results = []
    for i, post in enumerate(posts, 1):
        if len(results) >= n:
            break
        c = by_post.get(i, {})
        text = (c.get("fix") or "").strip() or post["text"]
        why = ("duplicate subject" if c.get("duplicate") else
               "fact check failed" if not (c.get("facts_ok") or c.get("fix")) else
               "no footage that shows it" if c.get("clip") not in cands else "")
        if why:
            log(f"[{niche}] skipped post {i} ({why}): {post.get('title', '')}")
            continue
        clip = cands[c["clip"]]
        if clip["id"] in used:
            continue
        used.add(clip["id"])
        secs = seconds_for(text)
        idx = sum(1 for m in made if m["niche"] == niche) + len(results) + 1
        out = out_dir / f"{niche}-{idx:03d}.mp4"
        card = card_image(text, post.get("highlight", []), f"{topic} Facts", f"@{niche}facts", colour)
        render(card, clip, pick_track(conn), secs, out)
        rec = {"niche": niche, "file": str(out), "title": post.get("title", ""), "text": text,
               "fixed": bool(c.get("fix")), "visual": clip["query"], "scene_id": clip["id"],
               "footage": clip["title"], "footage_source": clip["source"], "match": clip["score"],
               "seconds": round(secs, 1), "fact_source": post.get("source", ""),
               "made_at": datetime.now().isoformat(timespec="seconds")}
        results.append(rec)
        log(f"[{niche}] {out.name}: {rec['title']} ({secs:.0f} s, footage: {clip['title'][:50]})")
    done_file.write_text(json.dumps(made + results, indent=1, ensure_ascii=False), encoding="utf-8")
    return results


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--niche", choices=sorted(NICHES))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-n", type=int, default=3)
    a = ap.parse_args(argv)
    niches = sorted(NICHES) if a.all else [a.niche or "space"]
    total = []
    for i, niche in enumerate(niches, 1):
        log(f"niche {i}/{len(niches)}: {niche}")
        try:
            total += make(niche, a.n, [])
        except Exception as e:  # noqa: BLE001 - one niche failing must not stop the others
            log(f"[{niche}] failed: {e}")
    import tarfile
    tar = OUT.parent / "textcard.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        tf.add(OUT, arcname="textcard")
    log(f"done: {len(total)} Shorts in {OUT}, all samples packed in {tar}")


if __name__ == "__main__":
    main()
