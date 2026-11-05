"""Study one competitor channel (read-only): what it posts, how it performs, what its Shorts look like.

    python -m meta.study                 # every handle in meta/study.toml not studied yet

Steps: 1 channel + every upload with stats (YouTube API, a few units) -> 2 pick the top Shorts by
views plus the newest ones -> 3 download those with yt-dlp (research venv) at <=720p -> 4 per Short:
cuts per second, loudness, a frame sheet (1 frame/s), English captions if any, a 540x960 copy.
Results in ~/meta-out/study/: <handle>.json, sheets/*.jpg, videos/*.mp4. Never uploads or changes
anything; only hooks/titles/format patterns are used from it (never topics).
"""
from __future__ import annotations

import json
import re
import shutil
import statistics
import subprocess
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from meta.scan import chunks, seconds, service

HERE = Path(__file__).resolve().parent
OUT = Path.home() / "meta-out" / "study"
YTDLP = [str(Path.home() / "research-venv" / "bin" / "python"), "-m", "yt_dlp"]
NOW = datetime.now(timezone.utc)
TOP, NEWEST, LONG_CAPS = 8, 4, 3


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def channel(yt, handle: str) -> dict:
    res = yt.channels().list(part="snippet,statistics,contentDetails,brandingSettings",
                             forHandle=handle.lstrip("@")).execute()
    it = res["items"][0]
    sn, st = it["snippet"], it.get("statistics", {})
    return {"id": it["id"], "title": sn.get("title"), "handle": sn.get("customUrl"),
            "description": sn.get("description"), "created": sn.get("publishedAt"),
            "country": sn.get("country"), "keywords": it.get("brandingSettings", {}).get("channel", {}).get("keywords"),
            "subscribers": int(st.get("subscriberCount", 0) or 0), "views": int(st.get("viewCount", 0) or 0),
            "videos": int(st.get("videoCount", 0) or 0)}


def playlist_ids(yt, pid: str, pages: int = 20) -> list[str]:
    ids, token = [], None
    for _ in range(pages):
        try:
            res = yt.playlistItems().list(part="contentDetails", playlistId=pid, maxResults=50,
                                          pageToken=token).execute()
        except Exception:  # noqa: BLE001 - e.g. no Shorts playlist
            break
        ids += [i["contentDetails"]["videoId"] for i in res.get("items", [])]
        token = res.get("nextPageToken")
        if not token:
            break
    return ids


def videos(yt, ids: list[str], shorts: set[str]) -> list[dict]:
    out = []
    for part in chunks(ids):
        res = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(part)).execute()
        for it in res.get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            pub = sn.get("publishedAt")
            age = max((NOW - datetime.fromisoformat(pub.replace("Z", "+00:00"))).total_seconds() / 86400, 0.5)
            views = int(st.get("viewCount", 0) or 0)
            out.append({"id": it["id"], "short": it["id"] in shorts, "title": sn.get("title"),
                        "description": (sn.get("description") or "")[:1500], "tags": sn.get("tags", []),
                        "published": pub, "weekday_hour_utc": datetime.fromisoformat(pub.replace("Z", "+00:00")).strftime("%a %H:%M"),
                        "seconds": seconds(it["contentDetails"].get("duration")),
                        "lang": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
                        "category": sn.get("categoryId"), "views": views,
                        "likes": int(st.get("likeCount", 0) or 0), "comments": int(st.get("commentCount", 0) or 0),
                        "views_per_day": int(views / age)})
    out.sort(key=lambda v: v["published"] or "", reverse=True)
    return out


def vtt_text(path: Path) -> str:
    lines, last = [], None
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "-->" in ln or not ln.strip() or ln.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        t = re.sub(r"<[^>]+>", "", ln).strip()
        if t and t != last:
            lines.append(t)
            last = t
    return " ".join(lines)


def download(vid: str, tmp: Path, video: bool) -> tuple[Path | None, str, str]:
    """-> (mp4 or None, captions text, error)"""
    url = f"https://www.youtube.com/watch?v={vid}"
    cmd = YTDLP + ["-q", "--no-warnings", "--no-playlist", "--write-subs", "--write-auto-subs",
                   "--sub-langs", "en.*,en", "--sub-format", "vtt", "-o", str(tmp / f"{vid}.%(ext)s")]
    cmd += ["-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4"] if video else ["--skip-download"]
    try:
        p = run(cmd + [url], timeout=600)
    except subprocess.TimeoutExpired:
        return None, "", "yt-dlp timed out"
    subs = sorted(tmp.glob(f"{vid}*.vtt"))
    caps = vtt_text(subs[0]) if subs else ""
    mp4 = tmp / f"{vid}.mp4"
    return (mp4 if mp4.exists() else None), caps, (p.stderr.strip()[-400:] if p.returncode else "")


def measure(mp4: Path, vid: str) -> dict:
    """Cuts, loudness, frame sheet, small copy."""
    m: dict = {}
    p = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
             "-of", "json", str(mp4)])
    info = json.loads(p.stdout or "{}")
    dur = float(info.get("format", {}).get("duration", 0) or 0)
    vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    m["duration"] = round(dur, 1)
    m["size"] = f"{vs[0].get('width')}x{vs[0].get('height')}" if vs else None
    m["has_audio"] = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    p = run(["ffmpeg", "-hide_banner", "-i", str(mp4), "-vf", "scdet=threshold=12", "-an", "-f", "null", "-"], 600)
    cuts = [float(x) for x in re.findall(r"lavfi\.scd\.time:\s*([\d.]+)", p.stderr)]
    m["cuts"] = len(cuts)
    m["cut_times"] = [round(c, 1) for c in cuts]
    m["seconds_per_shot"] = round(dur / (len(cuts) + 1), 2) if dur else None
    if m["has_audio"]:
        p = run(["ffmpeg", "-hide_banner", "-i", str(mp4), "-af", "ebur128", "-vn", "-f", "null", "-"], 600)
        i = re.findall(r"I:\s*(-?[\d.]+) LUFS", p.stderr)
        m["lufs"] = float(i[-1]) if i else None
        # loudness per second: silence gaps vs constant bed (music only vs voice + music)
        p = run(["ffmpeg", "-hide_banner", "-i", str(mp4), "-af", "silencedetect=n=-40dB:d=0.4", "-vn", "-f", "null", "-"], 600)
        m["silences"] = len(re.findall(r"silence_start", p.stderr))
    sheets = OUT / "sheets"
    sheets.mkdir(parents=True, exist_ok=True)
    n = max(int(dur), 1)
    cols = 8
    rows = (n + cols - 1) // cols
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp4), "-vf",
         f"fps=1,scale=180:-2,tile={cols}x{rows}:padding=4:margin=4", "-frames:v", "1", "-q:v", "4",
         str(sheets / f"{vid}.jpg")], 600)
    first = sheets / f"{vid}-first3s.jpg"   # the hook: 6 frames over the first 3 seconds, larger
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-t", "3", "-i", str(mp4), "-vf",
         "fps=2,scale=300:-2,tile=6x1:padding=4:margin=4", "-frames:v", "1", "-q:v", "3", str(first)], 300)
    vids = OUT / "videos"
    vids.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp4), "-vf",
         "scale=540:960:force_original_aspect_ratio=decrease,pad=540:960:(ow-iw)/2:(oh-ih)/2",
         "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k",
         str(vids / f"{vid}.mp4")], 600)
    return m


def study(yt, handle: str) -> dict:
    ch = channel(yt, handle)
    log(f"{handle}: {ch['title']}, {ch['subscribers']} subs, {ch['videos']} videos, {ch['views']} views")
    up = playlist_ids(yt, "UU" + ch["id"][2:])
    sh = set(playlist_ids(yt, "UUSH" + ch["id"][2:]))
    vs = videos(yt, up, sh)
    log(f"{len(vs)} uploads read, {sum(v['short'] for v in vs)} Shorts")
    shorts = [v for v in vs if v["short"]]
    longs = [v for v in vs if not v["short"]]
    pick = sorted(shorts, key=lambda v: -v["views"])[:TOP]
    pick += [v for v in shorts[:NEWEST] if v not in pick]
    caps_only = sorted(longs, key=lambda v: -v["views"])[:LONG_CAPS]
    summary = {}
    for name, group in (("shorts", shorts), ("long", longs)):
        if group:
            vw = [v["views"] for v in group]
            summary[name] = {"count": len(group), "median_views": int(statistics.median(vw)),
                             "max_views": max(vw), "median_seconds": statistics.median(v["seconds"] for v in group),
                             "first": group[-1]["published"], "last": group[0]["published"]}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for i, v in enumerate(pick + caps_only, 1):
            video = v in pick
            log(f"{i}/{len(pick) + len(caps_only)}: {v['id']} ({v['views']} views) {'video' if video else 'captions'}")
            mp4, caps, err = download(v["id"], tmp, video)
            v["captions"] = caps[:6000]
            v["studied"] = True
            if err:
                v["download_error"] = err
                log(f"  yt-dlp: {err[-200:]}")
            if mp4:
                try:
                    v["measure"] = measure(mp4, v["id"])
                    log(f"  {v['measure'].get('duration')}s, {v['measure'].get('cuts')} cuts, "
                        f"{v['measure'].get('lufs')} LUFS, captions {len(caps)} chars")
                except (subprocess.SubprocessError, OSError, ValueError) as e:
                    log(f"  measuring failed: {e}")
                mp4.unlink(missing_ok=True)
    return {"studied_at": NOW.isoformat(timespec="seconds"), "channel": ch, "summary": summary, "videos": vs}


def main() -> None:
    handles = tomllib.loads((HERE / "study.toml").read_text(encoding="utf-8")).get("handles", [])
    OUT.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        log("ffmpeg missing")
    yt = service(None, None)
    for h in handles:
        dest = OUT / f"{h.lstrip('@').lower()}.json"
        if dest.exists():
            log(f"{h}: already studied ({dest.name})")
            continue
        try:
            data = study(yt, h)
        except Exception as e:  # noqa: BLE001 - one bad handle must not stop the rest
            log(f"{h}: failed: {e}")
            continue
        dest.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        log(f"{h}: written {dest}")


if __name__ == "__main__":
    main()
