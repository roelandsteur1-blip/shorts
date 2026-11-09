"""Clip-channel test Shorts (research/clip-channels.md): one test Short per footage type we may use.

For each type it searches archive.org for a fitting item, lets Claude pick the best 12-30 s
moment from frames (and, for TV with dialogue, a transcript), writes a hook, story captions and
a true payoff, and renders a 9:16 Short: hook on top, the film in a band, captions below.

Each type is rendered twice: "plain" (hook, captions, payoff) and "edited" (the same plus the
switchable clip-channel tricks from research/clip-engagement.md: sped-up setup, freeze + zoom on
the peak with a hit sound, slow-motion replay, a whoosh on the hook).

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
            f'mediatype:movies AND {PD} AND (creator:("Air Force") OR creator:(Navy) OR creator:("Photographic Group") '
            'OR creator:("Signal Corps")) AND (test OR experimental OR ejection OR landing OR crash OR rocket)',
            f'mediatype:movies AND {PD} AND creator:(NASA) AND (test OR landing OR crash OR rocket OR "lifting body")',
        ],
        "max_year": 1990,
        "rights": "US federal government work (Air Force/Navy/NASA film), public domain; item marked public domain.",
        "audio": "music",
        "gov_only": True,  # clips-1 picked a University of Kansas film: the maker must be a US federal agency
    },
}


GOV = ("air force", "navy", "army", "nasa", "department of defense", "u.s. government", "united states government",
       "marine corps", "national archives", "u.s. department", "naval", "usaf", "dod",
       # military film units (clips-2 skipped "Lookout Mountain Laboratory, 1352nd Photographic Group" = USAF)
       "photographic group", "photographic squadron", "lookout mountain", "signal corps", "army pictorial",
       "naval photographic", "air materiel command", "wright air development", "edwards air force")
NOT_GOV = ("university", "college", "inc.", "inc ", "company", "corporation", "corp.", "ltd", "foundation")


def gov_maker(meta: dict) -> bool:
    """True when the item's own metadata names a US federal maker and no university or company.
    (Being "about" the Air Force is not enough: clips-1 took a University of Kansas film.)"""
    def text(*keys):
        return " ".join(str(meta.get(k) or "") for k in keys).lower()
    maker = text("creator", "publisher", "sponsor", "producer", "contributor")
    if any(w in maker for w in NOT_GOV):
        return False
    return any(w in maker for w in GOV) or any(w in text("title", "description") for w in
                                                   ("produced by the u.s.", "u.s. air force film", "department of defense"))


PICK = """You are making a YouTube Short for a clip channel: {label}.
Footage: {brief}.
Archive item: "{title}" ({year}). Maker: {maker}. Description: {desc}

The image is a grid of {n} frames from the whole video, each labelled with its number and time.

1. Rights sanity check. Judge only from the frames: which decade does this footage look like it was
   filmed in ("decade_seen", e.g. 1920)? Is it colour? Does anything look newer than {year} (computer
   graphics, modern cars, colour video in a supposed 1920s film)? Set "era_ok" false if the footage
   is clearly newer than the stated year, or newer than {max_year}.
2. Pick the frame where the single best moment for a 12-30 second Short happens: action, a stunt, a
   fall, a surprise, something odd or funny that makes people stop scrolling. Prefer a moment where a
   person is clearly visible. Avoid title cards, credits, intertitles with only text, black frames.

Return JSON only: {{"decade_seen": 1950, "era_ok": true, "pick": 7, "why": "one line", "usable": true}}
Set "usable" false if nothing in this video would make a good Short."""

WRITE = """You are making a YouTube Short for a clip channel: {label}.
Footage: {brief}.
Archive item: "{title}" ({year}). Maker: {maker}. Description: {desc}

The image is a grid of frames from a {span:.0f}-second stretch of the video, one every {step:.0f} seconds;
each frame is labelled with its time in seconds from the start of this stretch.
Shot changes (cuts) in this stretch happen at: {cuts} seconds.{transcript}

1. Choose the moment: "start" and "end" (seconds from the start of this stretch), 12 to 30 seconds
   long. It must be ONE story about ONE subject: when a cut jumps to a different subject or story,
   end before it. No title cards. Start on a person or on the action, not on scenery.
   {audio_rule}
2. "peak": the second (from the start of this stretch) of the single most surprising instant (the
   fall, the crash, the reveal). "setup_end": the second where the build-up ends and things start
   to happen (between start and peak).
3. Our own layer on top (this is what makes it OUR Short, not a re-upload):
   - "hook": on-screen title, max 9 words, curiosity first, like "He tried to fix the roof and then
     THIS happened". No emoji.
   - "captions": 2 or 3 captions (max 10 words each). "at" = seconds from the chosen start when it
     appears. Each caption must describe what is ON SCREEN at that moment (look at the frame for that
     time) and tell the viewer what to watch for. "highlight": 1-2 key words of that caption.
   - "payoff": one TRUE fact about THIS clip specifically: who or what we see, where and when it was
     filmed, how the stunt was done, what happened next. Use the archive description and only facts
     you are sure of. NOT general trivia about the decade. Max 22 words.
   - "title": YouTube title, max 60 characters, may end with 1 emoji.
   - "credit": short source line, e.g. "Buster Keaton, One Week (1920)".
If no stretch here is good enough, set "usable" false.

Return JSON only:
{{"usable": true, "start": 4, "end": 24, "setup_end": 9, "peak": 15, "hook": "...",
"captions": [{{"at": 0, "text": "...", "highlight": ["word"]}}], "payoff": "...", "title": "...",
"credit": "...", "why": "one line"}}"""

REVIEW = """You check a YouTube Short before it is rendered. Clip channel: {label}.
Archive item: "{title}" ({year}). Description: {desc}{transcript}

The image shows the frames the viewer sees while each caption is on screen (labelled C1, C2, ...)
and while the payoff (P) is on screen.

Captions:
{lines}
Payoff: {payoff}

For each caption: does it match what its frame shows? If not, rewrite it (max 10 words) so it does,
keeping the story. Keep "highlight" 1-2 words from the new text.
For the payoff: is it a TRUE fact about THIS clip (not general era trivia)? If not, rewrite it from
the description, or about the film or event itself; max 22 words. If the P frame shows a different
subject than the rest, set "cut_end" true (we end the Short earlier).

Return JSON only: {{"captions": [{{"text": "...", "highlight": ["..."]}}], "payoff": "...",
"cut_end": false, "notes": "one line"}}"""


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


def research_venv() -> None:
    """faster-whisper lives in the research venv (meta/collector/tools.py), not the live one: add
    its packages at the END of the path, so live packages still win."""
    import sys
    for site in sorted((Path.home() / "research-venv" / "lib").glob("python*/site-packages")):
        if str(site) not in sys.path:
            sys.path.append(str(site))


def transcript(path: str) -> str:
    """Spoken words with times (faster-whisper on the CPU), or '' when it isn't installed."""
    research_venv()
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper not installed: no transcript")
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




def cuts(path: str, threshold: float = 0.3) -> list[float]:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo",
                        "-an", "-f", "null", "-"], capture_output=True, text=True, errors="replace", timeout=300)
    return [round(float(t), 1) for t in re.findall(r"pts_time:([0-9.]+)", p.stderr)]


# ---------- edit timeline ----------

SPEED_UP = 1.4      # the setup only, never the peak or dialogue
FREEZE = 0.7        # seconds the peak frame is held with a zoom punch-in
REPLAY_HALF = 0.8   # the replay shows peak +- this, at half speed


def timeline(plan: dict, edited: bool, keep_audio: bool) -> list[dict]:
    """Pieces in stretch time: {kind: play|freeze|replay, t0, t1, speed}. Plain = one piece."""
    start, end = plan["start"], plan["end"]
    if not edited:
        return [{"kind": "play", "t0": start, "t1": end, "speed": 1.0}]
    peak = min(max(plan.get("peak", start + (end - start) * 0.6), start + 1.0), end - 0.5)
    setup = min(max(plan.get("setup_end", start), start), peak)
    pieces = []
    if setup - start >= 3 and not keep_audio:
        pieces.append({"kind": "play", "t0": start, "t1": setup, "speed": SPEED_UP})
        pieces.append({"kind": "play", "t0": setup, "t1": peak, "speed": 1.0})
    else:
        pieces.append({"kind": "play", "t0": start, "t1": peak, "speed": 1.0})
    pieces.append({"kind": "freeze", "t0": peak, "t1": peak, "speed": 1.0})
    pieces.append({"kind": "play", "t0": peak, "t1": end, "speed": 1.0})
    if not keep_audio:
        pieces.append({"kind": "replay", "t0": max(start, peak - REPLAY_HALF), "t1": min(end, peak + REPLAY_HALF),
                       "speed": 0.5})
    return pieces


def piece_len(p: dict) -> float:
    return FREEZE if p["kind"] == "freeze" else (p["t1"] - p["t0"]) / p["speed"]


def out_time(pieces: list[dict], t: float) -> float:
    """Where stretch time t first plays in the output (play pieces only)."""
    acc = 0.0
    for p in pieces:
        if p["kind"] == "play" and p["t0"] <= t <= p["t1"]:
            return acc + (t - p["t0"]) / p["speed"]
        acc += piece_len(p)
    return acc


def build_band(src: str, pieces: list[dict], crop: str | None, band_h: int, keep_audio: bool, tmp: Path) -> Path:
    """The film band (W x band_h, 30 fps, with an audio track) cut to the timeline."""
    from shorts import media
    vf = f"{crop + ',' if crop else ''}scale={W}:{band_h},setsar=1"
    parts = []
    for i, p in enumerate(pieces):
        part = tmp / f"piece{i}.mp4"
        n = piece_len(p)
        if p["kind"] == "freeze":
            png = tmp / f"freeze{i}.png"
            media.run(["-ss", f"{p['t0']:.2f}", "-i", src, "-frames:v", "1", "-vf", vf, str(png)])
            frames = int(round(n * 30))
            zoom = (f"zoompan=z='1+0.18*min(on/{max(frames // 3, 1)},1)':d={frames}:x='iw/2-(iw/zoom/2)'"
                    f":y='ih/2-(ih/zoom/2)':s={W}x{band_h}:fps=30")
            media.run(["-i", str(png), "-f", "lavfi", "-t", f"{n:.2f}", "-i", "anullsrc=r=48000:cl=stereo",
                       "-vf", zoom, "-t", f"{n:.2f}", "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset",
                       "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", str(part)])
        else:
            v = f"{vf},setpts=PTS/{p['speed']},fps=30"
            args = ["-ss", f"{p['t0']:.2f}", "-t", f"{p['t1'] - p['t0']:.2f}", "-i", src]
            if keep_audio and p["speed"] == 1.0:
                args += ["-vf", v, "-af", "aresample=48000", "-map", "0:v", "-map", "0:a?"]
            else:
                args += ["-f", "lavfi", "-t", f"{n:.2f}", "-i", "anullsrc=r=48000:cl=stereo", "-vf", v,
                         "-map", "0:v", "-map", "1:a"]
            media.run(args + ["-t", f"{n:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                              "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", "-ar", "48000", str(part)])
        parts.append(part)
    listing = tmp / "pieces.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    band = tmp / "band.mp4"
    media.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "18", "-c:a", "aac", "-ar", "48000", str(band)])
    return band


def sfx(kind: str, out: Path) -> Path:
    """Synthetic sound effects (made by ffmpeg here, so no licence question)."""
    from shorts import media
    if kind == "whoosh":
        src = ("anoisesrc=d=0.7:c=pink:a=0.8,highpass=f=400,lowpass=f=4000,"
               "afade=t=in:d=0.35,afade=t=out:st=0.35:d=0.35,volume=0.9")
    else:  # hit: a falling low boom
        src = "aevalsrc='0.9*sin(2*PI*(70-45*t)*t)*exp(-5*t)|0.9*sin(2*PI*(70-45*t)*t)*exp(-5*t)':d=0.8:s=48000"
    media.run(["-f", "lavfi", "-i", src, "-ar", "48000", "-ac", "2", str(out)])
    return out


# ---------- text ----------

def text_block(text: str, size: int, colour, width: int = W - 120, box: bool = False,
               highlight: list[str] | None = None) -> Image.Image:
    text = re.sub(r"[\U00010000-\U0010FFFF☀-➿️]", "", text).strip()  # the font has no emoji
    f = font("bold", size)
    lines = wrap(text, f, width)
    lh = int(size * 1.28)
    keys = {w.lower().strip(".,!?\"':;") for h in (highlight or []) for w in str(h).split()}
    im = Image.new("RGBA", (W, lh * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(lines):
        y = 20 + i * lh
        tw = f.getlength(ln)
        if box:
            d.rounded_rectangle(((W - tw) / 2 - 18, y - 6, (W + tw) / 2 + 18, y + lh - 2), 14, fill=(0, 0, 0, 200))
        x = (W - tw) / 2
        for word in ln.split():
            hot = word.lower().strip(".,!?\"':;") in keys
            d.text((x, y), word, font=f, fill=(255, 221, 0) if hot else colour,
                   stroke_width=0 if box else 3, stroke_fill="black")
            x += f.getlength(word + " ")
    return im


def tag(text: str) -> Image.Image:
    f = font("bold", 40)
    tw = int(f.getlength(text))
    im = Image.new("RGBA", (tw + 40, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, tw + 40, 64), 12, fill=(220, 30, 30, 235))
    d.text((20, 8), text, font=f, fill="white")
    return im


# ---------- render ----------

def render(src: str, plan: dict, audio: str, track, out: Path, edited: bool = False) -> float:
    """One Short from the stretch `src` and the plan (stretch times). Returns its length."""
    from shorts import media
    keep_audio = audio == "original"
    pieces = timeline(plan, edited, keep_audio)
    total = sum(piece_len(p) for p in pieces)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        crop = black_bars(src, plan["start"])
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", src], capture_output=True, text=True)
        cw, ch = map(int, probe.stdout.strip().split(",")[:2])
        if crop:
            cw, ch = map(int, crop.split("=")[1].split(":")[:2])
        band_h = min(int(W * ch / cw), 900) // 2 * 2
        band_y = 640
        band = build_band(src, pieces, crop, band_h, keep_audio, tmp)
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        hook = text_block(plan["hook"], 70, "white")
        base.alpha_composite(hook, (0, max(150, band_y - hook.height - 50)))
        credit = text_block(plan.get("credit", ""), 30, (170, 170, 170))
        base.alpha_composite(credit, (0, band_y + band_h + 4))
        base.save(tmp / "base.png")
        inputs = ["-f", "lavfi", "-t", f"{total:.2f}", "-i", f"color=black:s={W}x{H}:r=30",
                  "-i", str(band), "-loop", "1", "-t", f"{total:.2f}", "-i", str(tmp / "base.png")]
        # captions in output time; the payoff comes last and stays to the end (hard cut, no fade)
        pay_at = max(total - 5.0, total * 0.65)
        timed = []
        for c in sorted(plan.get("captions", []), key=lambda c: float(c.get("at", 0))):
            if c.get("text"):
                at = out_time(pieces, plan["start"] + max(0.0, float(c.get("at", 0))))
                if at < pay_at - 1:
                    timed.append((at, c["text"], c.get("highlight") or []))
        timed.append((pay_at, plan["payoff"], []))
        fc = f"[0:v][1:v]overlay=0:{band_y}[a0];[a0][2:v]overlay=0:0[b0]"
        last, idx = "b0", 3
        for i, (at, text, hl) in enumerate(timed):
            end = timed[i + 1][0] if i + 1 < len(timed) else total + 1
            png = tmp / f"c{i}.png"
            payoff = i == len(timed) - 1
            text_block(text, 46 if payoff else 50, (255, 221, 0) if payoff else "white", box=True,
                       highlight=hl).save(png)
            inputs += ["-loop", "1", "-t", f"{total:.2f}", "-i", str(png)]
            fc += f";[{last}][{idx}:v]overlay=0:{band_y + band_h + 60}:enable='between(t,{at:.2f},{end:.2f})'[c{i}]"
            last, idx = f"c{i}", idx + 1
        events = []  # (sound, at)
        acc = 0.0
        for p in pieces:
            if p["kind"] == "freeze":
                events.append(("hit", acc))
            if p["kind"] == "replay":
                tpng = tmp / "replay.png"
                tag("REPLAY  0.5x").save(tpng)
                inputs += ["-loop", "1", "-t", f"{total:.2f}", "-i", str(tpng)]
                fc += f";[{last}][{idx}:v]overlay=40:{band_y + 30}:enable='between(t,{acc:.2f},{acc + piece_len(p):.2f})'[rp]"
                last, idx = "rp", idx + 1
            acc += piece_len(p)
        if edited:
            events.append(("whoosh", 0.0))
        fc += f";[{last}]format=yuv420p[out]"
        mix = []
        if keep_audio:
            fc += ";[1:a]loudnorm=I=-16:TP=-1.5,aresample=48000[main]"
        elif track:
            inputs += ["-ss", f"{track['start'] or 0:.2f}", "-t", f"{total:.2f}", "-i", track["path"]]
            fc += f";[{idx}:a]volume=0.55,aresample=48000[main]"
            idx += 1
        else:
            fc += ";[1:a]anull[main]"
        mix.append("[main]")
        for j, (kind, at) in enumerate(events):
            inputs += ["-i", str(sfx(kind, tmp / f"{kind}{j}.wav"))]
            fc += f";[{idx}:a]adelay={int(at * 1000)}|{int(at * 1000)},volume=0.8[s{j}]"
            mix.append(f"[s{j}]")
            idx += 1
        if len(mix) > 1:
            fc += f";{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first[aud]"
        else:
            fc += ";[main]anull[aud]"
        media.run(inputs + ["-filter_complex", fc, "-map", "[out]", "-map", "[aud]", "-t", f"{total:.2f}",
                            "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac",
                            "-b:a", "160k", "-movflags", "+faststart", str(out)])
    return total


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
            if tried >= 5:
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


def strip_frames(src: str, times: list[tuple[str, float]]) -> bytes:
    return grid([(lab, frame(src, t, 360)) for lab, t in times], cols=min(4, len(times)))


def try_item(key: str, spec: dict, doc: dict, settings, conn, llm) -> dict | None:
    ident = doc["identifier"]
    got = item_video(ident)
    if not got:
        log(f"[{key}] {ident}: no usable video file")
        return None
    url, length, meta = got
    if spec.get("gov_only") and not gov_maker(meta):
        log(f"[{key}] {ident}: skipped, maker is not a US federal agency ({meta.get('creator') or 'no creator'})")
        return None
    title = str(meta.get("title") or doc.get("title") or ident)
    year = year_of(meta) or year_of(doc) or "year unknown"
    maker = str(meta.get("creator") or meta.get("publisher") or "unknown")[:120]
    desc = re.sub(r"<[^>]+>|\s+", " ", str(meta.get("description") or ""))[:900]
    log(f"[{key}] {ident}: '{title}' ({year}), {length / 60:.1f} min, maker {maker}")
    lo, hi = min(15.0, length * 0.03), length * 0.97
    n = 30
    times = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    frames = [(f"#{i + 1} {mmss(t)}", frame(url, t)) for i, t in enumerate(times)]
    pick = llm.ask_json(PICK.format(label=spec["label"], brief=spec["brief"], title=title, year=year, maker=maker,
                                    desc=desc, n=n, max_year=spec["max_year"]),
                        settings.claude_cmd, retries=1, images=[grid(frames)])
    decade = pick.get("decade_seen")
    if pick.get("era_ok") is False or (isinstance(decade, int) and isinstance(year, int) and decade > year + 5) \
            or (isinstance(decade, int) and decade > spec["max_year"]):
        log(f"[{key}] {ident}: skipped, footage looks like the {decade}s, newer than its stated year {year}")
        return None
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
    keep_audio = spec["audio"] == "original"
    words = transcript(str(window)) if keep_audio else ""
    tx = f"\n\nWhat is said in this stretch (times from its start):\n{words}" if words else ""
    audio_rule = ("This clip keeps its ORIGINAL SOUND: start at the beginning of a line of dialogue and end "
                  "right after a punchline or reaction, never mid-sentence." if keep_audio else "")
    shot_cuts = cuts(str(window))
    plan = llm.ask_json(WRITE.format(label=spec["label"], brief=spec["brief"], title=title, year=year, maker=maker,
                                     desc=desc, span=span, step=step, transcript=tx, audio_rule=audio_rule,
                                     cuts=", ".join(f"{c:.0f}" for c in shot_cuts) or "none"),
                        settings.claude_cmd, retries=1, images=[grid(fine, cols=4)])
    if not plan.get("usable", True) or not plan.get("hook") or not plan.get("payoff"):
        log(f"[{key}] {ident}: no good stretch ({plan.get('why')})")
        return None
    start = min(max(0.0, float(plan.get("start", 0))), span - 12)
    end = min(float(plan.get("end", start + 20)), span)
    end = start + min(max(end - start, 12.0), 30.0, span - start)
    plan.update(start=start, end=end)
    for k in ("peak", "setup_end"):
        try:
            plan[k] = float(plan[k])
        except (KeyError, TypeError, ValueError):
            plan.pop(k, None)
    # review: the frames under each caption and under the payoff, so text matches the picture
    caps = [c for c in plan.get("captions", []) if c.get("text")]
    pay_t = start + (end - start) * 0.85
    labels = [(f"C{i + 1}", start + float(c.get("at", 0)) + 0.8) for i, c in enumerate(caps)] + [("P", pay_t)]
    lines = "\n".join(f"C{i + 1}: {c['text']}" for i, c in enumerate(caps))
    review = llm.ask_json(REVIEW.format(label=spec["label"], title=title, year=year, desc=desc, transcript=tx,
                                        lines=lines, payoff=plan["payoff"]),
                          settings.claude_cmd, retries=1, images=[strip_frames(str(window), labels)])
    new = review.get("captions") or []
    for c, r in zip(caps, new):
        if r.get("text"):
            c["text"], c["highlight"] = r["text"], r.get("highlight") or []
    plan["captions"] = caps
    if review.get("payoff"):
        plan["payoff"] = review["payoff"]
    if review.get("cut_end"):
        later = [c for c in shot_cuts if start + 12 <= c < end]
        if later:
            plan["end"] = later[0] - 0.1
    log(f"[{key}] review: {review.get('notes', '')}")
    track = pick_track(conn)
    lengths = {}
    for edited in (False, True):
        out = OUT / f"{key}-{'edited' if edited else 'plain'}.mp4"
        lengths[out.name] = round(render(str(window), plan, spec["audio"], track, out, edited=edited), 1)
    window.unlink(missing_ok=True)
    log(f"[{key}] made {', '.join(lengths)}: from {mmss(w0 + plan['start'])} of '{title}'")
    return {"type": key, "label": spec["label"], "identifier": ident, "link": f"https://archive.org/details/{ident}",
            "title_archive": title, "year": year, "maker": maker, "decade_seen": decade,
            "at": mmss(w0 + plan["start"]), "lengths": lengths, "rights": spec["rights"],
            "licenseurl": doc.get("licenseurl"), "audio": spec["audio"], "hook": plan["hook"],
            "captions": plan.get("captions"), "payoff": plan["payoff"], "peak": plan.get("peak"),
            "setup_end": plan.get("setup_end"), "yt_title": plan.get("title"), "credit": plan.get("credit"),
            "why": plan.get("why"), "review": review.get("notes"), "transcribed": bool(words)}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=sorted(TYPES))
    args = ap.parse_args(argv)
    from .common import open_library
    settings, conn, llm = open_library()
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.mp4"):
        old.unlink()
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
