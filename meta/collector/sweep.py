"""Footage collector research, steps R2 + R3: what today's library and every free archive offer
for the 60 test subjects (meta/collector/subjects.toml). Read-only: nothing is downloaded into the
library, nothing is written to the shorts database, nothing is uploaded except the job results.

Per subject and per source it records how many licence-OK hits a sharp search finds, the first
hits with their thumbnails, and a PE-Core score of each thumbnail against the subject (the same
model the library search uses). Output in ~/meta-out/sweep/:

    made.json                      all numbers (the runner copies it into the results repo)
    sheets/<niche>.jpg             round-1 review sheet: per subject, the best 2 thumbs per source
    sheets/subjects/<niche>-<n>.jpg  per subject: the first hits per source in archive order

    python -m meta.collector.sweep [--only niche] [--limit N]
    python -m meta.collector.sweep --extra   # only the keyed archives, into ~/meta-out/sweepx/
                                             # (for keys saved after the main sweep ran)

Keys are read from the live program's secrets (Sources page, api_keys.json): europeana, pixabay,
and the research-only keys dvids, smithsonian, nara. A source without a key is skipped and the
report says so. One source failing never stops the sweep.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw

from shorts.sources import license_from_name, license_from_url, nasa
from shorts.sources.common import UA

OUT = Path.home() / "meta-out" / "sweep"
SUBJECTS = Path(__file__).with_name("subjects.toml")
COMMONS = "https://commons.wikimedia.org/w/api.php"
WIKIDATA = "https://www.wikidata.org/w/api.php"
IA_SEARCH = "https://archive.org/advancedsearch.php"
SVS_SEARCH = "https://svs.gsfc.nasa.gov/api/search/"
DVIDS_SEARCH = "https://api.dvidshub.net/search"
SI_SEARCH = "https://api.si.edu/openaccess/api/v1.0/search"
NARA_SEARCH = "https://catalog.archives.gov/api/v2/records/search"
OPENVERSE = "https://api.openverse.org/v1/images/"
EUROPEANA = "https://api.europeana.eu/record/v2/search.json"
PIXABAY = "https://pixabay.com/api/videos/"
ROWS = 30      # hits asked per search
KEEP = 5       # licence-OK hits kept per source (thumbnails fetched and scored)
THUMB = 150

S = requests.Session()
S.headers.update(UA)

Get = Callable[..., dict]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get(url: str, headers: dict | None = None, **params) -> dict:
    """JSON GET with polite pacing and retries on 429/5xx. Raises after 3 tries."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = S.get(url, params=params, headers=headers, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            time.sleep(0.4)
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(3)
    raise RuntimeError(f"{url.split('?')[0]}: {last}")


def hit(title, ok: bool, licence, thumb, ref=None, kind: str = "video", **extra) -> dict:
    return {"title": str(title or "")[:90], "ok": bool(ok), "licence": licence, "thumb": thumb,
            "ref": ref, "kind": kind, **extra}


# ---------- Wikidata ----------

def pick_qid(results: list[dict], hint: str) -> tuple[str | None, str]:
    """The first of Wikidata's matches whose description contains the hint word (Wikidata's own top
    match is wrong for about 1 in 5 names: Saturn the god, lion the surname)."""
    for r in results:
        if hint.lower() in str(r.get("description", "")).lower():
            return r.get("id"), f"{r.get('label', '')}: {r.get('description', '')}"
    return None, "no match with '" + hint + "' in: " + "; ".join(str(r.get("description", "")) for r in results)


def qid(label: str, hint: str, g: Get = get) -> tuple[str | None, str]:
    return pick_qid(wikidata_matches(label, g), hint)


def wikidata_matches(label: str, g: Get = get) -> list[dict]:
    d = g(WIKIDATA, action="wbsearchentities", search=label, language="en", format="json", limit=5, type="item")
    return [{"id": r.get("id"), "label": r.get("label", ""), "description": r.get("description", "")}
            for r in d.get("search") or []]


PICK_QIDS = """For each subject below, pick the Wikidata item that IS the subject as a viewer of a short
video would mean it (the planet, not the god; the aircraft type, not a video game or a surname).
Answer "none" when no candidate fits.

{lines}

Return JSON only: {{"picks": {{"<subject number>": "<Q-id or none>"}}}}"""


def claude_qids(subjects: list[dict], matches: list[list[dict]], ask) -> dict[int, str | None]:
    """One Claude call for all subjects: {index: QID or None}. ask(prompt) -> parsed JSON."""
    lines = []
    for i, (s, ms) in enumerate(zip(subjects, matches), 1):
        opts = "; ".join(f"{m['id']} = {m['label']}: {m['description']}" for m in ms) or "no candidates"
        lines.append(f"{i}. {s['name']} (good shot: {s['show']}). Candidates: {opts}")
    picks = (ask(PICK_QIDS.format(lines="\n".join(lines))) or {}).get("picks") or {}
    out = {}
    for i, ms in enumerate(matches, 1):
        v = str(picks.get(str(i), "none"))
        out[i - 1] = v if v in {m["id"] for m in ms} else None
    return out


# ---------- the sources: each returns (total hits, [hit]) ----------

def commons(search: str, kind: str, g: Get = get, width: int = 320) -> tuple[int, list[dict]]:
    d = g(COMMONS, action="query", format="json", generator="search", gsrsearch=search, gsrnamespace=6,
          gsrlimit=ROWS, gsrinfo="totalhits", prop="imageinfo", iiprop="url|size|mime|extmetadata",
          iiurlwidth=width, iiextmetadatafilter="LicenseShortName|License|DateTimeOriginal|Artist")
    total = int(((d.get("query") or {}).get("searchinfo") or {}).get("totalhits") or 0)
    pages = sorted(((d.get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 0))
    hits = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        meta = {k: (v or {}).get("value", "") for k, v in (info.get("extmetadata") or {}).items()}
        lic = license_from_name(meta.get("License") or meta.get("LicenseShortName"))
        if lic.reject and meta.get("LicenseShortName"):
            lic = license_from_name(meta["LicenseShortName"])
        hits.append(hit(p.get("title", ""), not lic.reject, lic.license or lic.reject, info.get("thumburl"),
                        ref=info.get("descriptionurl") or p.get("title"), kind=kind,
                        seconds=info.get("duration"), height=info.get("height"), bytes=info.get("size"),
                        file=_commons_file(info), year=_year(meta.get("DateTimeOriginal")),
                        creator=_plain(meta.get("Artist"))))
    return total or len(hits), hits


def _plain(html) -> str:
    """Commons gives the author as HTML (a link to their user page): the name only."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(html or ""))).strip()[:120]


def _commons_file(info: dict) -> str | None:
    try:
        from shorts.sources import commons as cm
        return cm.pick_url(info) if info.get("url") else None
    except Exception:
        return info.get("url")


def _year(text) -> int | None:
    m = re.search(r"\b(18[89]\d|19\d\d|20[0-2]\d)\b", str(text or ""))
    return int(m.group(1)) if m else None


def internet_archive(q: str, base: str, g: Get = get) -> tuple[int, list[dict]]:
    query = f'(title:("{q}") OR subject:("{q}")) AND {base}'
    d = g(IA_SEARCH, q=query, rows=str(ROWS), output="json",
          **{"fl[]": ["identifier", "title", "licenseurl", "runtime"]})
    resp = d.get("response") or {}
    hits = []
    for doc in resp.get("docs") or []:
        lic = license_from_url(doc.get("licenseurl"))
        ident = doc.get("identifier", "")
        hits.append(hit(doc.get("title", ident), not lic.reject, lic.license or lic.reject,
                        f"https://archive.org/services/img/{quote(ident)}",
                        ref=f"https://archive.org/details/{ident}", runtime=doc.get("runtime")))
    return int(resp.get("numFound") or 0), hits


def nasa_video(q: str, g: Get = get) -> tuple[int, list[dict]]:
    d = g(f"{nasa.API}/search", q=q, media_type="video", page_size=ROWS)
    coll = d.get("collection") or {}
    hits = []
    for it in coll.get("items") or []:
        data = (it.get("data") or [{}])[0]
        thumb = next((l.get("href") for l in it.get("links") or [] if l.get("render") == "image"), None)
        flagged = nasa.needs_rights_check(data)
        hits.append(hit(data.get("title"), not flagged, "public-domain" + (" (needs check)" if flagged else ""),
                        thumb, ref=f"https://images.nasa.gov/details/{quote(str(data.get('nasa_id', '')))}",
                        nasa_id=data.get("nasa_id"), year=_year(data.get("date_created"))))
    return int((coll.get("metadata") or {}).get("total_hits") or 0), hits


def nasa_svs(q: str, g: Get = get) -> tuple[int, list[dict]]:
    """NASA Scientific Visualization Studio: visualisations and animations, mostly public domain."""
    d = g(SVS_SEARCH, search=q, limit=ROWS)
    rows = d.get("results") if isinstance(d, dict) else d
    hits = []
    for r in rows or []:
        hits.append(hit(r.get("title"), True, "public-domain (NASA SVS, credit check)", find_image(r),
                        ref=r.get("url") or f"https://svs.gsfc.nasa.gov/{r.get('id', '')}",
                        result_type=r.get("result_type") or r.get("page_type")))
    return int(d.get("count") or len(hits)) if isinstance(d, dict) else len(hits), hits


def find_image(obj, depth: int = 0) -> str | None:
    """The first .jpg/.png URL anywhere in an API record (field names differ per archive)."""
    if depth > 4:
        return None
    if isinstance(obj, str):
        low = obj.lower().split("?")[0]
        return obj if obj.startswith("http") and low.endswith((".jpg", ".jpeg", ".png")) else None
    items = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
    for v in items:
        got = find_image(v, depth + 1)
        if got:
            return got
    return None


def dvids(q: str, key: str, g: Get = get) -> tuple[int, list[dict]]:
    """DVIDS: US military media, public domain as US government work."""
    d = g(DVIDS_SEARCH, api_key=key, q=q, max_results=ROWS, **{"type[]": "video"})
    hits = []
    for r in d.get("results") or []:
        thumb = r.get("thumbnail")
        if isinstance(thumb, dict):
            thumb = thumb.get("url")
        hits.append(hit(r.get("title"), True, "public-domain (US government)", thumb,
                        ref=r.get("url"), seconds=r.get("duration")))
    total = (d.get("page_info") or {}).get("total_results") if isinstance(d.get("page_info"), dict) else None
    return int(total or len(hits)), hits


def smithsonian(q: str, key: str, g: Get = get) -> tuple[int, list[dict]]:
    """Smithsonian Open Access: CC0 media only (mostly photos, some video)."""
    d = g(SI_SEARCH, q=f"{q} AND online_media_type:*", api_key=key, rows=ROWS)
    resp = d.get("response") or {}
    hits = []
    for row in resp.get("rows") or []:
        content = row.get("content") or {}
        media = ((content.get("descriptiveNonRepeating") or {}).get("online_media") or {}).get("media") or []
        for m in media[:1]:
            cc0 = str((m.get("usage") or {}).get("access", "")).upper() == "CC0"
            kind = "video" if "video" in str(m.get("type", "")).lower() else "photo"
            hits.append(hit(row.get("title"), cc0, "CC0" if cc0 else (m.get("usage") or {}).get("access") or "no licence",
                            m.get("thumbnail"), ref=(content.get("descriptiveNonRepeating") or {}).get("record_link"),
                            kind=kind))
    return int(resp.get("rowCount") or len(hits)), hits


def nara(q: str, key: str, g: Get = get) -> tuple[int, list[dict]]:
    """US National Archives catalog: moving images that are online (mostly public domain)."""
    d = g(NARA_SEARCH, headers={"x-api-key": key}, q=q, typeOfMaterials="Moving Images",
          availableOnline="true", limit=ROWS)
    hh = ((d.get("body") or {}).get("hits") or {})
    hits = []
    for h in hh.get("hits") or []:
        rec = (h.get("_source") or {}).get("record") or {}
        objs = rec.get("digitalObjects") or []
        video = [o for o in objs if "video" in str(o.get("objectType", "")).lower()
                 or str(o.get("objectUrl", "")).lower().endswith((".mp4", ".mov", ".m4v"))]
        thumb = next((o.get("objectUrl") for o in objs if str(o.get("objectUrl", "")).lower().endswith((".jpg", ".png"))), None)
        restricted = str((rec.get("useRestriction") or {}).get("status", "")).lower()
        ok = bool(video) and restricted in ("", "unrestricted")
        hits.append(hit(rec.get("title"), ok, "public-domain (NARA, " + (restricted or "no restriction field") + ")",
                        thumb, ref=f"https://catalog.archives.gov/id/{rec.get('naId', '')}", files=len(video)))
    total = (hh.get("total") or {}).get("value") if isinstance(hh.get("total"), dict) else hh.get("total")
    return int(total or len(hits)), hits


def openverse(q: str, g: Get = get) -> tuple[int, list[dict]]:
    """Openverse: CC0 / public domain mark / CC BY photos from many collections (photo fallback)."""
    d = g(OPENVERSE, q=q, license="cc0,pdm,by", page_size=20)
    hits = [hit(r.get("title"), True, f"{r.get('license', '')} {r.get('license_version', '') or ''}".strip(),
                r.get("thumbnail"), ref=r.get("foreign_landing_url"), kind="photo", provider=r.get("provider"),
                file=r.get("url"), creator=str(r.get("creator") or "")[:120])
            for r in d.get("results") or []]
    return int(d.get("result_count") or len(hits)), hits


def europeana(q: str, key: str, g: Get = get) -> tuple[int, list[dict]]:
    d = g(EUROPEANA, wskey=key, query=q, qf="TYPE:VIDEO", reusability="open", media="true", rows=ROWS)
    hits = []
    for r in d.get("items") or []:
        rights = (r.get("rights") or [""])[0]
        lic = license_from_url(rights)
        hits.append(hit((r.get("title") or [""])[0], not lic.reject, lic.license or lic.reject,
                        (r.get("edmPreview") or [None])[0], ref=r.get("guid")))
    return int(d.get("totalResults") or len(hits)), hits


def pixabay(q: str, key: str, g: Get = get) -> tuple[int, list[dict]]:
    d = g(PIXABAY, key=key, q=q[:100], per_page=ROWS, safesearch="true")
    hits = []
    for r in d.get("hits") or []:
        v = r.get("videos") or {}
        thumb = next((v[k].get("thumbnail") for k in ("medium", "small", "large") if (v.get(k) or {}).get("thumbnail")), None)
        best = next((v[k].get("url") for k in ("medium", "large", "small") if (v.get(k) or {}).get("url")), None)
        hits.append(hit(r.get("tags"), True, "Pixabay Content License", thumb, ref=r.get("pageURL"), file=best))
    return int(d.get("totalHits") or len(hits)), hits


# (row label, function(subject, qid, keys) -> (total, hits) or None when it can't run, needs key)
def sources_for(keys: dict) -> list[tuple[str, Callable, str | None]]:
    return [
        ("Commons video (depicts)", lambda s, q: commons(f"haswbstatement:P180={q} filetype:video", "video") if q else None, None),
        ("Commons video (name)", lambda s, q: commons(f'"{s["name"]}" filetype:video', "video"), None),
        ("Internet Archive", lambda s, q: internet_archive(s["name"], "mediatype:(movies) AND licenseurl:*"), None),
        ("Prelinger", lambda s, q: internet_archive(s["name"], "mediatype:(movies) AND collection:(prelinger)"), None),
        ("NASA video", lambda s, q: nasa_video(s["name"]), None),
        ("NASA SVS", lambda s, q: nasa_svs(s["name"]), None),
        ("DVIDS", lambda s, q: dvids(s["name"], keys["dvids"]), "dvids"),
        ("NARA", lambda s, q: nara(s["name"], keys["nara"]), "nara"),
        ("Europeana", lambda s, q: europeana(s["name"], keys["europeana"]), "europeana"),
        ("Pixabay (B-roll)", lambda s, q: pixabay(s["name"], keys["pixabay"]), "pixabay"),
        ("Smithsonian", lambda s, q: smithsonian(s["name"], keys["smithsonian"]), "smithsonian"),
        ("Commons photos (depicts)", lambda s, q: commons(f"haswbstatement:P180={q} filetype:bitmap", "photo") if q else None, None),
        ("Openverse photos", lambda s, q: openverse(s["name"]), None),
    ]


# ---------- today's library (baseline) ----------

def library_hits(subject: dict, lib) -> tuple[int, list[dict]]:
    """What today's pipeline would find: the library search (PE when on) for the subject's name,
    the same way quiz/ranking look for clips (meta/textcard.find_clips)."""
    from meta.textcard import find_clips
    settings, conn = lib
    clips = find_clips(conn, settings, [subject["name"]], set(), need=3.0, per_query=KEEP)
    return len(clips), [hit(c["title"], True, c["source"], c["keyframe"], ref=f"scene {c['id']}",
                            score_lib=c["score"], file=c["path"], start=c["start"], end=c["end"],
                            height=c["height"]) for c in clips]


# ---------- thumbnails and scores ----------

def fetch_thumb(src: str | None) -> Image.Image | None:
    if not src:
        return None
    try:
        if src.startswith("http"):
            r = S.get(src, timeout=30)
            r.raise_for_status()
            im = Image.open(io.BytesIO(r.content))
        else:
            im = Image.open(src)
        im = im.convert("RGB")
        im.thumbnail((320, 320))
        return im
    except Exception:
        return None


class Scorer:
    """PE-Core B/16 similarity of a thumbnail to "a photo of <subject>"; None when the model
    can't load (the sweep still runs, sheets keep archive order)."""

    def __init__(self, settings):
        self.enc = None
        try:
            from shorts import embed
            self.enc = embed.get_pe(settings)
        except Exception as e:  # torch/open_clip missing or model download failed
            log(f"PE-Core not available, no scores: {e}")

    def scores(self, subject: dict, images: list[Image.Image]) -> list[float | None]:
        if not self.enc or not images:
            return [None] * len(images)
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, im in enumerate(images):
                p = Path(tmp) / f"{i}.jpg"
                im.save(p, quality=90)
                paths.append(str(p))
            iv = self.enc.images(paths)
        tv = self.enc.texts([f"a photo of {subject['name']}", subject["show"]])
        return [round(float(max(v @ tv[0], v @ tv[1])), 4) for v in iv]


# ---------- sheets ----------

def _label(d: ImageDraw.ImageDraw, xy, text: str, fill=(230, 230, 230)) -> None:
    d.text(xy, text, fill=fill)


def subject_sheet(subject: dict, rows: list[tuple[str, list[dict]]], path: Path) -> None:
    """Per subject: one row per source, its first licence-OK hits in archive order."""
    cell, lh, left = THUMB + 6, 26, 190
    W, H = left + KEEP * cell, 50 + len(rows) * (cell + lh)
    img = Image.new("RGB", (W, H), (22, 22, 22))
    d = ImageDraw.Draw(img)
    _label(d, (10, 10), f"{subject['niche']} / {subject['name']}  (good shot: {subject['show']})", (255, 255, 255))
    y = 50
    for name, hits in rows:
        _label(d, (10, y + cell // 2 - 8), name, (255, 210, 110))
        for i, h in enumerate(hits[:KEEP]):
            x = left + i * cell
            im = h.get("_img")
            if im:
                t = im.copy()
                t.thumbnail((THUMB, THUMB))
                img.paste(t, (x, y))
            sc = h.get("score")
            _label(d, (x, y + THUMB + 4), f"{i + 1}" + (f"  pe {sc:.2f}" if sc is not None else "") +
                   ("  photo" if h.get("kind") == "photo" else ""), (190, 190, 190))
        y += cell + lh
    img.save(path, quality=82)


def niche_sheet(niche: str, block: list[tuple[dict, list[tuple[str, list[dict]]]]], names: list[str], path: Path) -> None:
    """Round-1 review sheet: rows = subjects, columns = sources, each cell the 2 best-scoring
    licence-OK thumbs (what a collector would pick first), numbered for the score sheet."""
    per, tw = 2, 120
    cw, rh, top, left = per * (tw + 4) + 10, tw + 30, 70, 170
    W, H = left + len(names) * cw, top + len(block) * rh
    img = Image.new("RGB", (W, H), (22, 22, 22))
    d = ImageDraw.Draw(img)
    _label(d, (10, 10), f"{niche}: best 2 thumbnails per source (licence OK). Label = subject.source.pick", (255, 255, 255))
    for j, n in enumerate(names):
        _label(d, (left + j * cw, 40), n[:22], (255, 210, 110))
    for i, (subject, rows) in enumerate(block):
        y = top + i * rh
        _label(d, (10, y + tw // 2 - 8), subject["name"][:24], (255, 255, 255))
        got = dict(rows)
        for j, n in enumerate(names):
            hits = [h for h in got.get(n, []) if h.get("_img")]
            hits.sort(key=lambda h: -(h.get("score") or 0))
            for k, h in enumerate(hits[:per]):
                t = h["_img"].copy()
                t.thumbnail((tw, tw))
                x = left + j * cw + k * (tw + 4)
                img.paste(t, (x, y))
                _label(d, (x, y + tw + 2), f"{i + 1}.{j + 1}.{k + 1}", (170, 170, 170))
    img.save(path, quality=82)


# ---------- main ----------

def load_subjects(only: str | None = None, limit: int | None = None) -> list[dict]:
    subs = tomllib.loads(SUBJECTS.read_text(encoding="utf-8"))["subject"]
    if only:
        subs = [s for s in subs if s["niche"] == only]
    return subs[:limit] if limit else subs


def load_keys(settings) -> dict:
    from shorts import sources
    return {n: sources.api_key(settings, n) for n in ("dvids", "nara", "europeana", "pixabay", "smithsonian")}


ASK = None  # Claude (llm.ask_json) once the library is open; None in tests / without Claude


def open_library():
    try:
        from meta.common import open_library as ol
        settings, conn, llm = ol()
        global ASK
        ASK = lambda prompt: llm.ask_json(prompt, settings.claude_cmd, retries=1)
        return settings, conn
    except Exception as e:
        log(f"library not available, no baseline: {e}")
        return None


def sweep_subject(s: dict, q: str | None, row: dict, lib, srcs, keys: dict, scorer) -> list[tuple[str, list[dict]]]:
    """Search every source for one subject (library first when lib is given), fetch and score the
    thumbnails; fills row["sources"] and returns [(source name, hits)] for the sheets."""
    rows: list[tuple[str, list[dict]]] = []
    if lib:
        try:
            t0 = time.time()
            total, hits = library_hits(s, lib)
            rows.append(("Library today", hits))
            row["sources"]["Library today"] = {"found": total, "seconds": round(time.time() - t0, 1)}
        except Exception as e:
            row["sources"]["Library today"] = {"error": str(e)[:300]}
    for name, fn, need in srcs:
        if need and not keys.get(need):
            continue
        t0 = time.time()
        try:
            got = fn(s, q)
        except Exception as e:  # one archive failing never stops the sweep
            row["sources"][name] = {"error": str(e)[:300], "seconds": round(time.time() - t0, 1)}
            log(f"  {name}: error {str(e)[:160]}")
            continue
        if got is None:
            row["sources"][name] = {"skipped": "no Wikidata id"}
            continue
        total, hits = got
        ok = [h for h in hits if h["ok"]]
        row["sources"][name] = {"total": total, "checked": len(hits), "licence_ok": len(ok),
                                "rejected": sorted({str(h["licence"]) for h in hits if not h["ok"]})[:5],
                                "seconds": round(time.time() - t0, 1)}
        rows.append((name, ok[:KEEP]))
    # thumbnails + scores
    for name, hits in rows:
        for h in hits:
            h["_img"] = fetch_thumb(h.get("thumb"))
        imgs = [h["_img"] for h in hits if h["_img"]]
        for h, sc in zip([h for h in hits if h["_img"]], scorer.scores(s, imgs) if scorer.enc else [None] * len(imgs)):
            h["score"] = sc
        row["sources"].setdefault(name, {})["hits"] = [
            {k: v for k, v in h.items() if k != "_img"} | {"thumb_ok": h["_img"] is not None} for h in hits]
    return rows


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="one niche")
    ap.add_argument("--limit", type=int, help="first N subjects")
    ap.add_argument("--extra", action="store_true", help="only archives that need a key, no library baseline")
    a = ap.parse_args(argv)
    global OUT
    if a.extra:
        OUT = OUT.with_name("sweepx")
    try:
        os.nice(10)  # the live pipeline goes first
    except OSError:
        pass
    (OUT / "sheets" / "subjects").mkdir(parents=True, exist_ok=True)
    subjects = load_subjects(a.only, a.limit)
    lib = open_library()
    keys = load_keys(lib[0]) if lib else {}
    scorer = Scorer(lib[0]) if lib else Scorer.__new__(Scorer)
    if not lib:
        scorer.enc = None
    srcs = sources_for(keys)
    if a.extra:
        srcs = [x for x in srcs if x[2]]
    skipped = sorted({k for _, _, k in srcs if k and not keys.get(k)})
    if skipped:
        log(f"no key yet, skipped: {', '.join(skipped)}")
    names = ([] if a.extra else ["Library today"]) + [n for n, _, k in srcs if not k or keys.get(k)]
    matches = []
    for s in subjects:
        try:
            matches.append(wikidata_matches(s["wikidata"]))
        except Exception as e:
            log(f"Wikidata lookup failed for {s['name']}: {e}")
            matches.append(None)
    picked = None
    if ASK:
        try:
            picked = claude_qids(subjects, [m or [] for m in matches], ASK)
            log(f"Claude picked Wikidata items for {sum(1 for v in picked.values() if v)} of {len(subjects)} subjects")
        except Exception as e:
            log(f"Claude Wikidata pick failed, using hint words: {e}")
    results, by_niche = [], {}
    t_all = time.time()
    for idx, s in enumerate(subjects, 1):
        log(f"{idx}/{len(subjects)} {s['niche']} / {s['name']}")
        row = {"niche": s["niche"], "name": s["name"], "sources": {}}
        ms = matches[idx - 1]
        if ms is None:
            q, desc = None, "Wikidata lookup failed"
        elif picked is not None and picked.get(idx - 1):
            q = picked[idx - 1]
            m = next(m for m in ms if m["id"] == q)
            desc = f"{m['label']}: {m['description']} (Claude)"
        else:
            q, desc = pick_qid(ms, s["hint"])
        row["qid"], row["qid_label"] = q, desc
        rows = sweep_subject(s, q, row, lib if not a.extra else None, srcs, keys, scorer)
        slug = re.sub(r"[^a-z0-9]+", "_", s["name"].lower()).strip("_")
        subject_sheet(s, rows, OUT / "sheets" / "subjects" / f"{s['niche']}-{slug}.jpg")
        by_niche.setdefault(s["niche"], []).append((s, rows))
        summary = {n: (v.get("licence_ok", v.get("found")) if "error" not in v else "ERR")
                   for n, v in row["sources"].items()}
        log(f"  qid {q} ({desc[:50]}); usable: {json.dumps(summary)}")
        results.append(row)
        (OUT / "made.json").write_text(json.dumps({"subjects": results, "skipped_no_key": skipped,
                                                   "minutes": round((time.time() - t_all) / 60, 1)}, indent=1),
                                       encoding="utf-8")
    for niche, block in by_niche.items():
        niche_sheet(niche, block, names, OUT / "sheets" / f"{niche}.jpg")
    log(f"done: {len(results)} subjects in {(time.time() - t_all) / 60:.0f} min")


if __name__ == "__main__":
    main()
