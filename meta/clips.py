"""Clip-channel test Shorts (research/clip-channels.md): one test Short per footage type we may use.

For each type it searches archive.org for a fitting item, lets Claude pick the best 12-30 s
moment from frames (and, for TV with dialogue, a transcript), writes a hook, story captions and
a true payoff, and renders a 9:16 Short: hook on top, the film in a band, captions below.

Samples only: MP4s go to ~/meta-out/clips/, with made.json listing each item, its link, the
rights basis and the texts. Nothing is uploaded and the `shorts` database is only read (music).

    python -m meta.clips                 # every type
    python -m meta.clips --only silent   # one type
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageDraw

from .textcard import H, W, black_bars, font, log, pick_track, wrap

OUT = Path.home() / "meta-out" / "clips"
SEARCH = "https://archive.org/advancedsearch.php"
UA = {"User-Agent": "shorts-pipeline/1.0 (research samples)"}
VIDEO_EXT = (".mp4", ".m4v", ".mpeg", ".mpg", ".ogv", ".avi", ".mov", ".webm")
PD = "licenseurl:(*publicdomain*)"

# key -> what the channel would be, archive.org searches (tried in order), the rights basis we
# rely on (never the uploader's label alone), and whether the original sound is kept.
TYPES = {
    "silent": {
        "label": "Silent comedy stunts (old school fails)",
        "brief": "silent slapstick comedy from the 1920s: stunts, falls, chases, near misses and clever gags",
        "searches": [
            'title:("One Week") AND Keaton AND mediatype:movies',
            'title:("The Scarecrow") AND Keaton AND mediatype:movies',
            'title:("The Electric House") AND Keaton AND mediatype:movies',
            'title:("Never Weaken") AND Lloyd AND mediatype:movies',
            'title:("Cops") AND Keaton AND mediatype:movies',
        ],
        "max_year": 1930,
        "rights": "US public domain by age (published 1930 or earlier). Original score removed.",
        "audio": "music",
    },
    "newsreel": {
        "label": "Strange news from the past (newsreels)",
        "brief": ("a 1930s-1960s newsreel: odd inventions, daredevils, stunts going wrong, strange fashions "
                  "and contests, 'they really did this' moments"),
        "searches": [
            'title:("Universal Newsreel") AND (stunt OR daredevil OR odd OR invention OR crazy OR novel) AND mediatype:movies',
            'collection:(universal_newsreels) AND (stunt OR daredevil OR invention OR odd)',
            'title:("Universal Newsreel") AND mediatype:movies',
        ],
        "max_year": 1967,
        "rights": "Universal Newsreel: MCA gave the newsreels to the US National Archives without copyright restrictions (1974).",
        "audio": "music",
    },
    "ads": {
        "label": "Weird vintage TV commercials",
        "brief": "a vintage 1950s-1960s TV commercial or advertising film: odd claims, strange products, 'they really sold this'",
        "searches": [
            f'collection:(prelinger) AND (subject:(commercials) OR subject:(television commercials) OR title:(commercial)) AND {PD}',
            f'collection:(prelinger) AND subject:(advertising) AND {PD}',
        ],
        "max_year": 1977,
        "rights": "Prelinger Archives item marked public domain (pre-1978 ad).",
        "audio": "music",
    },
    "homemovies": {
        "label": "Life back then (old home movies)",
        "brief": "an amateur home movie from the 1920s-1960s: everyday life, parties, kids, cars, beaches, how people lived",
        "searches": [
            f'collection:(prelinger) AND (subject:("home movies") OR title:("home movies")) AND {PD}',
            f'collection:(prelinger) AND (subject:(amateur) OR title:(amateur)) AND {PD}',
        ],
        "max_year": 1977,
        "rights": "Prelinger Archives home movie marked public domain.",
        "audio": "music",
    },
    "classictv": {
        "label": "Classic TV moments (with original sound)",
        "brief": "a 1950s TV sitcom episode: a funny scene with a clear setup and punchline",
        "searches": [
            'title:("Life with Elizabeth") AND mediatype:movies',
            'title:("Burns and Allen") AND mediatype:movies',
            'title:("Jack Benny") AND mediatype:movies',
        ],
        "max_year": 1963,
        "rights": ("TEST ONLY: 1950s episode widely circulated as public domain; NOT yet checked per "
                   "episode (renewal records) as the report requires before any upload."),
        "audio": "original",
    },
    "military": {
        "label": "Military test footage (for warplane)",
        "brief": "US military or NASA test film: ejection seats, rocket sleds, crash tests, carrier landings, test flights",
        "searches": [
            f'mediatype:movies AND {PD} AND (title:("ejection seat") OR title:("rocket sled") OR subject:(ejection))',
            f'mediatype:movies AND {PD} AND (title:("crash test") OR title:("test flight")) AND ("Air Force" OR Navy OR NASA)',
        ],
        "max_year": 1990,
        "rights": "US federal government work (Air Force/Navy/NASA film), public domain; item marked public domain.",
        "audio": "music",
    },
}

PICK = """You are making a YouTube Short for a clip channel: {label}.
Footage: {brief}.
Archive item: "{title}" ({year}). Description: {desc}

The image is a grid of {n} frames from the whole video, each labelled with its number and time.
Pick the frame where the single best moment for a 12-30 second Short starts or happens: action,
a stunt, a fall, a surprise, something odd or funny that makes people stop scrolling. Avoid title
cards, credits, intertitles with only text, black frames and talking heads.

Return JSON only: {{"pick": 7, "why": "one line", "usable": true}}
Set "usable" false if nothing in this video would make a good Short."""

WRITE = """You are making a YouTube Short for a clip channel: {label}.
Footage: {brief}.
Archive item: "{title}" ({year}). Description: {desc}

The image is a grid of frames from a {span:.0f}-second stretch of the video, one every {step:.0f} seconds;
each frame is labelled with its time in seconds from the start of this stretch.{transcript}

1. Choose the moment: "start" and "end" in seconds from the start of this stretch, 12 to 30 seconds
   long, with a clear setup and payoff. Stay inside one scene where you can; no title cards.
2. Write our own layer on top (this is what makes it OUR Short, not a re-upload):
   - "hook": on-screen title, max 9 words, curiosity first, style of "He tried to fix the roof and
     then THIS happened" (1 emoji at the end allowed).
   - "captions": 2 or 3 short story captions (max 12 words each) with "at" = seconds from the
     chosen start when each appears. They tell the viewer what to watch for; never describe the
     obvious.
   - "payoff": one TRUE, well-documented fact about this film, the people in it, or the era, max 22
     words (e.g. Keaton did his own stunts). Only facts you are sure of; else something general and
     true about the era.
   - "title": YouTube title, max 60 characters.
   - "credit": short source line, e.g. "Buster Keaton, One Week (1920)".
If no stretch here is good enough, set "usable" false.

Return JSON only:
{{"usable": true, "start": 4, "end": 24, "hook": "...", "captions": [{{"at": 0, "text": "..."}}],
"payoff": "...", "title": "...", "credit": "...", "why": "one line"}}"""


# ---------- archive.org ----------

def search(query: str, rows: int = 15) -> list[dict]:
    import requests
    params = [("q", query), ("rows", str(rows)), ("output", "json"), ("sort[]", "downloads desc")]
    params += [("fl[]", f) for f in ("identifier", "title", "year", "date", "licenseurl", "description",
                                       "downloads", "collection")]
    r = requests.get(SEARCH, params=params, headers=UA, timeout=60)
    r.raise_for_status()
    return r.json().get("response", {}).get("docs", [])


def year_of(doc: dict) -> int | None:
    for v in (doc.get("year"), doc.get("date")):
        m = re.search(r"(1[89]\d\d|20\d\d)", str(v or ""))
        if m:
            return int(m.group(1))
    return None


def length_seconds(v) -> float | None:
    """archive.org 'length' is seconds ('123.4') or 'mm:ss' / 'hh:mm:ss'."""
    if v is None:
        return None
    s = str(v).strip()
    try:
        if ":" in s:
            secs = 0.0
            for part in s.split(":"):
                secs = secs * 60 + float(part)
            return secs
        return float(s)
    except ValueError:
        return None


def pick_file(files: list[dict]) -> tuple[str, float | None] | None:
    """(file name, length) of the best video file: an h.264 derivative first, else the smallest
    original video under 2 GB."""
    vids = [f for f in files if str(f.get("name", "")).lower().endswith(VIDEO_EXT)]
    if not vids:
        return None

    def rank(f):
        name = f["name"].lower()
        fmt = str(f.get("format", "")).lower()
        h264 = name.endswith(".mp4") and ("h.264" in fmt or "mpeg4" in fmt or "512kb" in name)
        return (0 if h264 else 1, 0 if name.endswith(".mp4") else 1, int(f.get("size") or 1e12))
    best = sorted(vids, key=rank)[0]
    if int(best.get("size") or 0) > 2_000_000_000:
        return None
    return best["name"], length_seconds(best.get("length"))


def item_video(ident: str) -> tuple[str, float, dict] | None:
    import requests
    r = requests.get(f"https://archive.org/metadata/{quote(ident)}", headers=UA, timeout=60)
    r.raise_for_status()
    meta = r.json()
    got = pick_file(meta.get("files", []))
    if not got:
        return None
    name, length = got
    url = f"https://archive.org/download/{quote(ident)}/{quote(name)}"
    length = length or probe_length(url)
    if not length or length < 20:
        return None
    return url, length, meta.get("metadata", {})


def probe_length(url: str) -> float | None:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", url],
                       capture_output=True, text=True, timeout=120)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return None


# ---------- frames ----------

def frame(url_or_path: str, t: float, width: int = 320) -> Image.Image | None:
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{max(0, t):.2f}", "-i", url_or_path,
                        "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "-"],
                       capture_output=True, timeout=180)
    if not p.stdout:
        return None
    try:
        return Image.open(io.BytesIO(p.stdout)).convert("RGB")
    except OSError:
        return None


def grid(frames: list[tuple[str, Image.Image | None]], cols: int = 6) -> bytes:
    """One JPEG with the frames in a grid, each labelled."""
    ok = [(lab, im) for lab, im in frames if im is not None]
    if not ok:
        raise RuntimeError("no frames")
    w = max(im.width for _, im in ok)
    h = max(im.height for _, im in ok)
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (w + 8), rows * (h + 34)), "white")
    d = ImageDraw.Draw(sheet)
    f = font("bold", 22)
    for i, (lab, im) in enumerate(frames):
        x, y = (i % cols) * (w + 8), (i // cols) * (h + 34)
        d.text((x + 4, y + 4), lab, font=f, fill="black")
        if im is not None:
            sheet.paste(im, (x, y + 30))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def transcript(path: str) -> str:
    """Spoken words with times (faster-whisper on the CPU), or '' when it isn't installed."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return ""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-vn", "-f", "s16le",
                        "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", "-"], capture_output=True, timeout=600)
    audio = np.frombuffer(p.stdout, np.int16).astype(np.float32) / 32768.0
    if not len(audio):
        return ""
    model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=4)
    segs, _ = model.transcribe(audio, language="en")
    return "\n".join(f"[{s.start:.0f}-{s.end:.0f}s] {s.text.strip()}" for s in segs)


# ---------- render ----------

def text_block(text: str, size: int, colour, width: int = W - 120, box: bool = False) -> Image.Image:
    text = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F]", "", text).strip()  # the font has no emoji
    f = font("bold", size)
    lines = wrap(text, f, width)
    lh = int(size * 1.28)
    im = Image.new("RGBA", (W, lh * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(lines):
        y = 20 + i * lh
        if box:
            tw = f.getlength(ln)
            d.rounded_rectangle(((W - tw) / 2 - 18, y - 6, (W + tw) / 2 + 18, y + lh - 2), 14, fill=(0, 0, 0, 200))
        d.text((W / 2, y), ln, font=f, fill=colour, anchor="ma", stroke_width=0 if box else 3,
               stroke_fill="black")
    return im


def render(src: str, start: float, seconds: float, plan: dict, audio: str, track, out: Path) -> None:
    from shorts import media
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        crop = black_bars(src, start)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", src], capture_output=True, text=True)
        cw, ch = map(int, probe.stdout.strip().split(",")[:2])
        if crop:
            cw, ch = map(int, crop.split("=")[1].split(":")[:2])
        band_h = min(int(W * ch / cw), 900) // 2 * 2
        band_y = 640
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        hook = text_block(plan["hook"], 70, "white")
        base.alpha_composite(hook, (0, max(150, band_y - hook.height - 50)))
        credit = text_block(plan.get("credit", ""), 30, (170, 170, 170))
        base.alpha_composite(credit, (0, band_y + band_h + 4))
        base.save(tmp / "base.png")
        inputs = ["-f", "lavfi", "-t", f"{seconds:.2f}", "-i", f"color=black:s={W}x{H}:r=30",
                  "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}", "-i", src,
                  "-loop", "1", "-t", f"{seconds:.2f}", "-i", str(tmp / "base.png")]
        caps = sorted(plan.get("captions", []), key=lambda c: float(c.get("at", 0)))
        pay_at = max(seconds - 5.0, seconds * 0.65)
        timed = [(max(0.0, float(c.get("at", 0))), c["text"]) for c in caps if c.get("text")]
        timed = [(a, t) for a, t in timed if a < pay_at - 1]
        timed.append((pay_at, plan["payoff"]))
        fc = (f"[1:v]{crop + ',' if crop else ''}scale={W}:{band_h},setsar=1,fps=30[v];"
              f"[0:v][v]overlay=0:{band_y}[a0];[a0][2:v]overlay=0:0[b0]")
        last = "b0"
        for i, (at, text) in enumerate(timed):
            end = timed[i + 1][0] if i + 1 < len(timed) else seconds + 1
            png = tmp / f"c{i}.png"
            text_block(text, 50 if i < len(timed) - 1 else 46, (255, 221, 0) if i == len(timed) - 1 else "white",
                       box=True).save(png)
            inputs += ["-loop", "1", "-t", f"{seconds:.2f}", "-i", str(png)]
            fc += f";[{last}][{3 + i}:v]overlay=0:{band_y + band_h + 60}:enable='between(t,{at:.2f},{end:.2f})'[c{i}]"
            last = f"c{i}"
        fc += f";[{last}]format=yuv420p[out]"
        n = 3 + len(timed)
        if audio == "original":
            fc += ";[1:a]loudnorm=I=-16:TP=-1.5,aresample=48000[aud]"
        elif track:
            inputs += ["-ss", f"{track['start'] or 0:.2f}", "-t", f"{seconds:.2f}", "-i", track["path"]]
            fc += f";[{n}:a]volume=0.55,aresample=48000[aud]"
        else:
            inputs += ["-f", "lavfi", "-t", f"{seconds:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
            fc += f";[{n}:a]aresample=48000[aud]"
        media.run(inputs + ["-filter_complex", fc, "-map", "[out]", "-map", "[aud]", "-t", f"{seconds:.2f}",
                            "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac",
                            "-b:a", "160k", "-movflags", "+faststart", str(out)])


# ---------- one type ----------

def make_one(key: str, spec: dict, settings, conn, llm, used: set[str]) -> dict | None:
    tried = 0
    for q in spec["searches"]:
        try:
            docs = search(q)
        except Exception as e:  # noqa: BLE001 - one bad search shouldn't end the type
            log(f"[{key}] search failed: {e}")
            continue
        log(f"[{key}] {len(docs)} items for: {q}")
        for doc in docs:
            ident = doc["identifier"]
            year = year_of(doc)
            if ident in used or (year and year > spec["max_year"]):
                continue
            if tried >= 4:
                return None
            tried += 1
            try:
                made = try_item(key, spec, doc, settings, conn, llm)
            except Exception as e:  # noqa: BLE001
                log(f"[{key}] {ident} failed: {e}")
                made = None
            if made:
                used.add(ident)
                return made
    return None


def try_item(key: str, spec: dict, doc: dict, settings, conn, llm) -> dict | None:
    ident = doc["identifier"]
    got = item_video(ident)
    if not got:
        log(f"[{key}] {ident}: no usable video file")
        return None
    url, length, meta = got
    title = str(meta.get("title") or doc.get("title") or ident)
    year = year_of(meta) or year_of(doc) or "year unknown"
    desc = re.sub(r"<[^>]+>|\s+", " ", str(meta.get("description") or ""))[:600]
    log(f"[{key}] {ident}: '{title}' ({year}), {length / 60:.1f} min")
    lo, hi = min(15.0, length * 0.03), length * 0.97
    n = 30
    times = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    frames = [(f"#{i + 1} {mmss(t)}", frame(url, t)) for i, t in enumerate(times)]
    pick = llm.ask_json(PICK.format(label=spec["label"], brief=spec["brief"], title=title, year=year, desc=desc,
                                    n=n), settings.claude_cmd, retries=1, images=[grid(frames)])
    if not pick.get("usable", True):
        log(f"[{key}] {ident}: Claude found no good moment ({pick.get('why')})")
        return None
    idx = min(max(int(pick.get("pick", 1)) - 1, 0), n - 1)
    w0 = max(0.0, times[idx] - 20)
    span = min(64.0, length - w0)
    OUT.mkdir(parents=True, exist_ok=True)
    window = OUT / f"{key}.shot.mp4"  # a local copy of the stretch, so the render doesn't seek over HTTP
    from shorts import media
    media.run(["-ss", f"{w0:.2f}", "-t", f"{span:.2f}", "-i", url, "-vf", "scale=-2:min(720\\,ih)",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "160k", str(window)])
    step = 4.0
    fine = [(f"{t:.0f}s", frame(str(window), t)) for t in [i * step for i in range(int(span // step))]]
    words = transcript(str(window)) if spec["audio"] == "original" else ""
    tx = f"\n\nWhat is said in this stretch (times from its start):\n{words}" if words else ""
    plan = llm.ask_json(WRITE.format(label=spec["label"], brief=spec["brief"], title=title, year=year, desc=desc,
                                     span=span, step=step, transcript=tx),
                        settings.claude_cmd, retries=1, images=[grid(fine, cols=4)])
    if not plan.get("usable", True) or not plan.get("hook") or not plan.get("payoff"):
        log(f"[{key}] {ident}: no good stretch ({plan.get('why')})")
        return None
    start = min(max(0.0, float(plan.get("start", 0))), span - 12)
    end = min(float(plan.get("end", start + 20)), span)
    seconds = min(max(end - start, 12.0), 30.0, span - start)
    out = OUT / f"{key}.mp4"
    render(str(window), start, seconds, plan, spec["audio"], pick_track(conn), out)
    window.unlink(missing_ok=True)
    log(f"[{key}] made {out.name}: {seconds:.0f} s from {mmss(w0 + start)} of '{title}'")
    return {"type": key, "label": spec["label"], "identifier": ident, "link": f"https://archive.org/details/{ident}",
            "title_archive": title, "year": year, "at": mmss(w0 + start), "seconds": round(seconds, 1),
            "rights": spec["rights"], "licenseurl": doc.get("licenseurl"), "audio": spec["audio"],
            "hook": plan["hook"], "captions": plan.get("captions"), "payoff": plan["payoff"],
            "yt_title": plan.get("title"), "credit": plan.get("credit"), "why": plan.get("why"),
            "file": out.name, "transcribed": bool(words)}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=sorted(TYPES))
    args = ap.parse_args(argv)
    from .common import open_library
    settings, conn, llm = open_library()
    OUT.mkdir(parents=True, exist_ok=True)
    made, used = [], set()
    for key, spec in TYPES.items():
        if args.only and key != args.only:
            continue
        log(f"=== {spec['label']}")
        m = make_one(key, spec, settings, conn, llm, used)
        made.append(m or {"type": key, "label": spec["label"], "failed": True})
        (OUT / "made.json").write_text(json.dumps(made, indent=1, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for m in made if not m.get("failed"))
    log(f"done: {ok} of {len(made)} types made")


if __name__ == "__main__":
    main()
