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
- Each post needs an archive video that fits. Describe it as a short visual search query of what
  can be SEEN (e.g. "radio telescope dish", "Saturn rings", "B-17 bomber in flight"), not an idea.
- Different subjects for every post. Avoid: {avoid}.

Return JSON only:
{{"posts": [{{"text": "...", "highlight": ["1-3 key words from the text"], "visual": "...",
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


def seconds_for(text: str) -> float:
    return max(6.0, min(15.0, len(text.split()) / 2.7 + 1.5))


def find_clip(conn, settings, query: str, used: set[int], need: float):
    """Best scene for the query whose video file exists and runs long enough from the scene start."""
    from shorts import embed
    for sid, score in embed.search(conn, settings, query, k=40, exclude=used):
        r = conn.execute("""SELECT sc.id, sc.start, sc.end, a.path, a.duration, a.title, a.source, a.width,
                                   a.height FROM scenes sc JOIN assets a ON a.id = sc.asset_id
                             WHERE sc.id=?""", (sid,)).fetchone()
        if not r or not r["path"] or not Path(r["path"]).exists():
            continue
        if (r["duration"] or 0) - r["start"] < need or (r["width"] or 0) < (r["height"] or 1):
            continue
        return dict(r) | {"score": round(score, 3)}
    return None


def pick_track(conn):
    rows = conn.execute("SELECT path, start FROM tracks WHERE status='ok'").fetchall()
    rows = [r for r in rows if r["path"] and Path(r["path"]).exists()]
    return random.choice(rows) if rows else None


def render(card: Image.Image, clip: dict, track, seconds: float, out: Path) -> None:
    from shorts import media
    vid_h = int(W * clip["height"] / clip["width"]) // 2 * 2
    total = card.height + 24 + vid_h
    top = max(240, (H - total) // 2 - 60)  # a little above centre: bottom of the screen has the UI
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    canvas.paste(card, (0, top))
    png = out.with_suffix(".png")
    canvas.save(png)
    args = ["-loop", "1", "-i", str(png), "-ss", f"{clip['start']:.2f}", "-t", f"{seconds:.2f}",
            "-i", clip["path"]]
    if track:
        args += ["-ss", f"{track['start'] or 0:.2f}", "-t", f"{seconds:.2f}", "-i", track["path"]]
    else:
        args += ["-f", "lavfi", "-t", f"{seconds:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
    fc = (f"[1:v]scale={W}:{vid_h},setsar=1,fps=30[v];"
          f"[0:v][v]overlay=0:{top + card.height + 24}:shortest=1,format=yuv420p[out];"
          f"[2:a]volume=0.55,aresample=48000[a]")
    args += ["-filter_complex", fc, "-map", "[out]", "-map", "[a]", "-t", f"{seconds:.2f}",
             "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    media.run(args)
    png.unlink(missing_ok=True)


def make(niche: str, n: int, seed_avoid: list[str]) -> list[dict]:
    from shorts import db, llm
    from shorts.config import load_settings
    settings = load_settings()
    conn = db.connect(settings.db_path)
    topic, hot = NICHES[niche]
    out_dir = OUT / niche
    out_dir.mkdir(parents=True, exist_ok=True)
    done_file = OUT / "made.json"
    made = json.loads(done_file.read_text(encoding="utf-8")) if done_file.exists() else []
    avoid = [m["title"] for m in made if m["niche"] == niche] + seed_avoid
    log(f"[{niche}] asking Claude for {n + 2} posts")
    data = llm.ask_json(PROMPT.format(topic=topic, hot=hot, n=n + 2,
                                      avoid="; ".join(avoid[-40:]) or "nothing yet"),
                        settings.claude_cmd, retries=1)
    used = {m["scene_id"] for m in made}
    colour = random.Random(niche).choice([(232, 65, 24), (29, 155, 240), (0, 186, 124),
                                          (120, 86, 255), (249, 24, 128)])
    results = []
    for post in data.get("posts", []):
        if len(results) >= n:
            break
        text = re.sub(r"\s+", " ", post["text"]).strip()
        secs = seconds_for(text)
        clip = find_clip(conn, settings, post["visual"], used, secs)
        if not clip:
            log(f"[{niche}] no fitting footage for '{post['visual']}', skipped")
            continue
        used.add(clip["id"])
        idx = sum(1 for m in made if m["niche"] == niche) + len(results) + 1
        out = out_dir / f"{niche}-{idx:03d}.mp4"
        card = card_image(text, post.get("highlight", []), f"{topic} Facts",
                          f"@{niche}facts", colour)
        render(card, clip, pick_track(conn), secs, out)
        rec = {"niche": niche, "file": str(out), "title": post.get("title", ""), "text": text,
               "visual": post["visual"], "scene_id": clip["id"], "footage": clip["title"],
               "footage_source": clip["source"], "match": clip["score"], "seconds": round(secs, 1),
               "fact_source": post.get("source", ""), "made_at": datetime.now().isoformat(timespec="seconds")}
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
