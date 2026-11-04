"""Archive probe for the topic-first plan (read-only research, no downloads into the library).

For a fixed list of nameable topics it measures, per archive and search method, how many usable
(licence OK) videos and photos a SHARP search finds, saves one contact sheet per topic with the
first frames of the top hits (to judge by eye how many really show the subject), and tests
cutting an 8-second piece out of a remote video without downloading the whole file.

    python -m meta.probe            # ~10-20 minutes, writes ~/meta-out/probe/

Nothing is stored in the shorts database and nothing is uploaded anywhere except the job results.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw

from shorts.sources import commons, internet_archive, license_from_name, license_from_url, nasa
from shorts.sources.common import UA

OUT = Path.home() / "meta-out" / "probe"
SHEETS = OUT / "sheets"
COMMONS = "https://commons.wikimedia.org/w/api.php"
WIKIDATA = "https://www.wikidata.org/w/api.php"
IA_SEARCH = "https://archive.org/advancedsearch.php"
PER = 6        # hits per method shown on a sheet
THUMB = 240

# (niche, topic as a person would name it, Wikidata search label)
TOPICS = [
    ("space", "Saturn", "Saturn"),
    ("space", "Space Shuttle launch", "Space Shuttle"),
    ("space", "Apollo 11", "Apollo 11"),
    ("warplane", "P-51 Mustang", "North American P-51 Mustang"),
    ("warplane", "B-17 Flying Fortress", "Boeing B-17 Flying Fortress"),
    ("warplane", "SR-71 Blackbird", "Lockheed SR-71 Blackbird"),
    ("ocean", "humpback whale", "humpback whale"),
    ("ocean", "octopus", "Octopus"),
    ("ocean", "sea turtle", "sea turtle"),
    ("planet", "African elephant", "African bush elephant"),
    ("planet", "lion", "lion"),
    ("planet", "bald eagle", "bald eagle"),
    ("history", "Hindenburg", "LZ 129 Hindenburg"),
    ("history", "Golden Gate Bridge", "Golden Gate Bridge"),
    ("history", "Statue of Liberty", "Statue of Liberty"),
    ("retrotech", "steam locomotive", "steam locomotive"),
    ("retrotech", "typewriter", "typewriter"),
    ("weather", "tornado", "tornado"),
    ("earthquake", "volcano eruption", "volcanic eruption"),
    ("oldcity", "Times Square", "Times Square"),
]

S = requests.Session()
S.headers.update(UA)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get(url: str, **params) -> dict:
    for attempt in range(3):
        try:
            r = S.get(url, params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            time.sleep(0.5)
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(3)
    raise RuntimeError(f"{url}: {last}")


# ---------- Wikidata ----------

def qid(label: str) -> tuple[str | None, str]:
    d = get(WIKIDATA, action="wbsearchentities", search=label, language="en", format="json", limit=1, type="item")
    hit = (d.get("search") or [{}])[0]
    return hit.get("id"), f"{hit.get('label', '')}: {hit.get('description', '')}"


# ---------- Commons ----------

def commons_search(search: str, limit: int = 30) -> tuple[int, list[dict]]:
    """(total hits, first results with licence verdict + thumbnail url)."""
    d = get(COMMONS, action="query", format="json", list="search", srsearch=search, srnamespace=6,
            srlimit=1, srinfo="totalhits")
    total = int(((d.get("query") or {}).get("searchinfo") or {}).get("totalhits") or 0)
    d = get(COMMONS, action="query", format="json", generator="search", gsrsearch=search, gsrnamespace=6,
            gsrlimit=limit, prop="imageinfo", iiprop="url|size|mime|extmetadata", iiurlwidth=THUMB,
            iiextmetadatafilter="LicenseShortName|License")
    pages = sorted(((d.get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 0))
    hits = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        meta = {k: (v or {}).get("value", "") for k, v in (info.get("extmetadata") or {}).items()}
        lic = license_from_name(meta.get("License") or meta.get("LicenseShortName"))
        if lic.reject and meta.get("LicenseShortName"):
            lic = license_from_name(meta["LicenseShortName"])
        hits.append({"title": p.get("title", ""), "ok": not lic.reject, "licence": lic.license or lic.reject,
                     "thumb": info.get("thumburl"), "url": info.get("url"), "bytes": info.get("size"),
                     "seconds": info.get("duration"), "height": info.get("height"),
                     "file_url": commons.pick_url(info) if info.get("url") else None})
    return total, hits


# ---------- NASA ----------

def nasa_search(q: str) -> tuple[int, list[dict]]:
    d = get(f"{nasa.API}/search", q=q, media_type="video", page_size=30)
    coll = d.get("collection") or {}
    total = int((coll.get("metadata") or {}).get("total_hits") or 0)
    hits = []
    for it in coll.get("items") or []:
        data = (it.get("data") or [{}])[0]
        thumb = next((l.get("href") for l in it.get("links") or [] if l.get("render") == "image"), None)
        flagged = nasa.needs_rights_check(data)
        hits.append({"title": data.get("title", ""), "id": data.get("nasa_id"), "ok": True,
                     "licence": "public-domain" + (" (needs check)" if flagged else ""), "thumb": thumb})
    return total, hits


# ---------- Internet Archive ----------

def ia_search(q: str, licensed: bool) -> tuple[int, list[dict]]:
    query = f"({q}) AND mediatype:(movies)" + (" AND licenseurl:*" if licensed else "")
    params = [("q", query), ("rows", "30"), ("output", "json"),
              ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "licenseurl"), ("fl[]", "runtime")]
    r = S.get(IA_SEARCH, params=params, timeout=60)
    r.raise_for_status()
    time.sleep(0.5)
    resp = r.json().get("response") or {}
    hits = []
    for doc in resp.get("docs") or []:
        lic = license_from_url(doc.get("licenseurl"))
        ident = doc["identifier"]
        hits.append({"title": str(doc.get("title", ident))[:80], "id": ident, "ok": not lic.reject,
                     "licence": lic.license or lic.reject, "runtime": doc.get("runtime"),
                     "thumb": f"https://archive.org/services/img/{quote(ident)}"})
    return int(resp.get("numFound") or 0), hits


def ia_thumbs(ident: str) -> dict:
    """How many preview frames the archive already made for this item (the <id>.thumbs folder)."""
    d = get(f"https://archive.org/metadata/{quote(ident)}")
    files = d.get("files") or []
    thumbs = [f for f in files if f.get("format") == "Thumbnail"]
    vids = [f for f in files if str(f.get("name", "")).lower().endswith(internet_archive.VIDEO_EXT)]
    longest = max((internet_archive.seconds(f.get("length")) or 0 for f in vids), default=0)
    return {"thumbs": len(thumbs), "video_seconds": round(longest), "files": len(files)}


# ---------- contact sheets ----------

def fetch_thumb(url: str | None) -> Image.Image | None:
    if not url:
        return None
    try:
        r = S.get(url, timeout=30)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((THUMB, THUMB))
        return im
    except Exception:
        return None


def sheet(topic: str, rows: list[tuple[str, list[dict]]], path: Path) -> None:
    """One row per search method, PER usable hits each; label = method and hit number."""
    cell, label_h = THUMB + 8, 22
    W = 150 + PER * cell
    H = len(rows) * (cell + label_h) + 40
    img = Image.new("RGB", (W, H), (25, 25, 25))
    d = ImageDraw.Draw(img)
    d.text((10, 10), topic, fill=(255, 255, 255))
    y = 40
    for method, hits in rows:
        d.text((10, y + cell // 2), method, fill=(255, 220, 120))
        for i, h in enumerate(hits[:PER]):
            im = fetch_thumb(h.get("thumb"))
            x = 150 + i * cell
            if im:
                img.paste(im, (x, y))
            d.text((x, y + THUMB + 2), f"{i + 1}", fill=(200, 200, 200))
        y += cell + label_h
    img.save(path, quality=80)


# ---------- partial cut ----------

def cut(url: str, at: float, seconds: float = 8) -> dict:
    """Cut `seconds` at `at` out of a remote file with ffmpeg; report time and bytes read from
    the network (the process' rchar, which counts socket reads) against the full file size."""
    out = {"url": url}
    try:
        h = S.head(url, allow_redirects=True, timeout=30)
        out["accept_ranges"] = h.headers.get("Accept-Ranges", "")
        out["full_bytes"] = int(h.headers.get("Content-Length") or 0)
    except requests.RequestException as e:
        out["head_error"] = str(e)[:200]
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "piece.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.1f}", "-i", url,
               "-t", str(seconds), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(dst)]
        start = time.time()
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        read = [0]

        def watch():
            while p.poll() is None:
                try:
                    for line in Path(f"/proc/{p.pid}/io").read_text().splitlines():
                        if line.startswith("rchar:"):
                            read[0] = int(line.split()[1])
                except OSError:
                    pass
                time.sleep(0.2)

        t = threading.Thread(target=watch, daemon=True)
        t.start()
        try:
            _, err = p.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            p.kill()
            err = b"timeout"
        t.join(1)
        out["seconds_taken"] = round(time.time() - start, 1)
        out["bytes_read"] = read[0]
        out["ok"] = p.returncode == 0 and dst.exists() and dst.stat().st_size > 10_000
        if not out["ok"]:
            out["error"] = err.decode(errors="replace")[-300:]
    if out.get("full_bytes"):
        out["share_read"] = round(out["bytes_read"] / out["full_bytes"], 4)
    return out


def duration(url: str) -> float | None:
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", url],
                           capture_output=True, text=True, timeout=120)
        return float(p.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None


# ---------- main ----------

def main() -> None:
    SHEETS.mkdir(parents=True, exist_ok=True)
    results, cut_urls = [], []
    for niche, topic, label in TOPICS:
        log(f"{topic} ({niche})")
        row = {"niche": niche, "topic": topic}
        try:
            q, desc = qid(label)
            row["qid"], row["qid_label"] = q, desc
            rows = []
            total, hits = commons_search(f'"{topic}" filetype:video')
            row["commons_text_video"] = {"total": total, "usable": sum(h["ok"] for h in hits), "checked": len(hits)}
            rows.append(("Commons text", [h for h in hits if h["ok"]]))
            if q:
                total, dhits = commons_search(f"haswbstatement:P180={q} filetype:video")
                row["commons_depicts_video"] = {"total": total, "usable": sum(h["ok"] for h in dhits), "checked": len(dhits)}
                rows.append(("Commons depicts", [h for h in dhits if h["ok"]]))
                total, ihits = commons_search(f"haswbstatement:P180={q} filetype:bitmap")
                row["commons_depicts_photo"] = {"total": total, "usable": sum(h["ok"] for h in ihits), "checked": len(ihits)}
                rows.append(("Commons photos", [h for h in ihits if h["ok"]]))
                cut_urls += [("commons", h["file_url"], h.get("seconds")) for h in dhits if h["ok"] and h.get("file_url")][:1]
            total, nhits = nasa_search(topic)
            row["nasa_video"] = {"total": total, "usable": len(nhits)}
            if niche == "space":
                rows.append(("NASA", nhits))
                if nhits:
                    cut_urls.append(("nasa", nhits[0]["id"], None))
            total_all, _ = ia_search(f'title:("{topic}")', licensed=False)
            total, ahits = ia_search(f'title:("{topic}")', licensed=True)
            usable = [h for h in ahits if h["ok"]]
            row["ia_video"] = {"total_any_licence": total_all, "with_licence": total, "usable": len(usable)}
            if usable:
                row["ia_thumbs_example"] = {"id": usable[0]["id"], **ia_thumbs(usable[0]["id"])}
                cut_urls.append(("ia", usable[0]["id"], None))
            rows.append(("Internet Arch.", usable))
            sheet(topic, rows, SHEETS / f"{niche}-{topic.replace(' ', '_')}.jpg")
        except Exception as e:  # one bad topic shouldn't end the probe
            row["error"] = str(e)[:300]
            log(f"  error: {e}")
        log(f"  {json.dumps({k: v for k, v in row.items() if k not in ('niche', 'topic')})}")
        results.append(row)
        (OUT / "made.json").write_text(json.dumps({"topics": results}, indent=1), encoding="utf-8")

    log("partial cuts")
    cuts, seen = [], set()
    for kind, ref, secs in cut_urls:
        if len([c for c in cuts if c["kind"] == kind]) >= 3 or ref in seen:
            continue
        seen.add(ref)
        try:
            if kind == "nasa":
                url = nasa.pick_rendition(nasa.asset_files(ref, S))
            elif kind == "ia":
                url = internet_archive.file_url(internet_archive.Item(source="internet_archive", source_id=ref), S)
            else:
                url = ref
            if not url:
                continue
            secs = secs or duration(url)
            at = (secs or 60) * 0.4
            c = {"kind": kind, "video_seconds": round(secs or 0), **cut(url, at)}
        except Exception as e:
            c = {"kind": kind, "ref": ref, "error": str(e)[:300]}
        log(f"  {json.dumps(c)}")
        cuts.append(c)
        (OUT / "made.json").write_text(json.dumps({"topics": results, "cuts": cuts}, indent=1), encoding="utf-8")
    log("done")


if __name__ == "__main__":
    main()
