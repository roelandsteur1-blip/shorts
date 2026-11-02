"""Scan which Shorts get the most views right now (research plan: plan/youtube-meta-research-plan.md).

Read-only. Uses one channel's saved YouTube login (or an API key) only to READ public data; it changes
nothing in the `shorts` program, its database or its channels.

    python -m meta.scan                     # everything, results in ~/meta-scan
    python -m meta.scan --budget 6000       # stop before using more API units than this
    python -m meta.scan --no-extras         # skip cover images and auto-captions

Steps: 1 search (100 units each) -> 2 channels (their Shorts of the last 90 days, ~1 unit per 50)
-> 3 scores (outlier = views / median of the channel's last 30 Shorts) -> 4 extras for the outliers
(cover image, auto-captions via yt-dlp if installed) -> meta-scan.tar.gz to send back.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOW = datetime.now(timezone.utc)
RESERVE = 1500  # units kept for reading the channels after the searches


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Budget:
    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def spend(self, units: int) -> bool:
        if self.used + units > self.limit:
            return False
        self.used += units
        return True


def service(channel_id: str | None, api_key: str | None):
    from googleapiclient.discovery import build
    if api_key:
        return build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    from shorts import upload
    from shorts.config import all_channels, load_settings
    settings = load_settings()
    chans = [c for c in all_channels() if channel_id in (None, c.id)]
    for ch in chans:
        if upload.token_path(settings, ch.id).exists():
            log(f"reading with the saved YouTube login of channel '{ch.id}' (read-only calls)")
            return build("youtube", "v3", credentials=upload.credentials(settings, ch, interactive=False),
                         cache_discovery=False)
    sys.exit("no channel has a saved YouTube login; pass --api-key")


def seconds(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def when(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def discover(yt, budget: Budget, days: int) -> tuple[dict, list]:
    """Search per niche: most-viewed short videos of the last `days` days. -> {channel_id: info}, hits"""
    queries = tomllib.loads((HERE / "queries.toml").read_text(encoding="utf-8"))["q"]
    after = (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found: dict[str, dict] = {}
    hits = []
    total = sum(len(q["terms"]) for q in queries)
    n = 0
    for q in queries:
        for term in q["terms"]:
            n += 1
            if budget.used + 100 > budget.limit - RESERVE or not budget.spend(100):
                log(f"search {n}/{total}: search budget reached (keeping {RESERVE} units for step 2)")
                return found, hits
            try:
                res = yt.search().list(part="snippet", q=term, type="video", videoDuration="short",
                                       order="viewCount", publishedAfter=after, maxResults=50,
                                       regionCode="US", relevanceLanguage="en").execute()
            except Exception as e:  # noqa: BLE001 - one bad search must not stop the scan
                log(f"search {n}/{total} '{term}' failed: {e}")
                continue
            items = res.get("items", [])
            for rank, it in enumerate(items):
                cid = it["snippet"]["channelId"]
                f = found.setdefault(cid, {"niches": set(), "hits": 0, "best_rank": 99,
                                           "title": it["snippet"].get("channelTitle")})
                f["niches"].add(q["niche"])
                f["hits"] += 1
                f["best_rank"] = min(f["best_rank"], rank)
                hits.append({"niche": q["niche"], "term": term, "rank": rank,
                             "video_id": it["id"]["videoId"], "channel_id": cid})
            log(f"search {n}/{total} [{q['niche']}] '{term}': {len(items)} videos, "
                f"{len(found)} channels so far, {budget.used} units used")
    return found, hits


def chunks(xs, n=50):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def channel_info(yt, budget: Budget, ids: list[str]) -> dict:
    out = {}
    for part in chunks(ids):
        if not budget.spend(1):
            break
        res = yt.channels().list(part="snippet,statistics", id=",".join(part), maxResults=50).execute()
        for it in res.get("items", []):
            st = it.get("statistics", {})
            out[it["id"]] = {"title": it["snippet"].get("title"),
                             "handle": it["snippet"].get("customUrl"),
                             "created": it["snippet"].get("publishedAt"),
                             "country": it["snippet"].get("country"),
                             "subscribers": int(st.get("subscriberCount", 0) or 0),
                             "videos": int(st.get("videoCount", 0) or 0),
                             "views": int(st.get("viewCount", 0) or 0)}
    return out


def channel_shorts(yt, budget: Budget, cid: str, days: int, pages: int) -> list[dict]:
    """The channel's Shorts (the 'UUSH' Shorts-only playlist), newest first, with stats."""
    ids, token = [], None
    cutoff = NOW - timedelta(days=days)
    for _ in range(pages):
        if not budget.spend(1):
            break
        try:
            res = yt.playlistItems().list(part="contentDetails", playlistId="UUSH" + cid[2:],
                                          maxResults=50, pageToken=token).execute()
        except Exception:  # noqa: BLE001 - channel without a Shorts playlist
            break
        items = res.get("items", [])
        ids += [it["contentDetails"]["videoId"] for it in items]
        token = res.get("nextPageToken")
        if not token or (items and when(items[-1]["contentDetails"].get("videoPublishedAt", NOW.isoformat()))
                         < cutoff - timedelta(days=60)):
            break
    vids = []
    for part in chunks(ids):
        if not budget.spend(1):
            break
        res = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(part)).execute()
        for it in res.get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            vids.append({"id": it["id"], "channel_id": cid, "title": sn.get("title"),
                         "description": (sn.get("description") or "")[:600],
                         "tags": sn.get("tags", [])[:15], "published": sn.get("publishedAt"),
                         "seconds": seconds(it["contentDetails"].get("duration")),
                         "lang": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
                         "views": int(st.get("viewCount", 0) or 0),
                         "likes": int(st.get("likeCount", 0) or 0),
                         "comments": int(st.get("commentCount", 0) or 0)})
    vids.sort(key=lambda v: v["published"] or "", reverse=True)
    return vids


def score(channels: dict, shorts: dict[str, list[dict]], days: int) -> list[dict]:
    cutoff = NOW - timedelta(days=days)
    rows = []
    for cid, vids in shorts.items():
        last30 = [v["views"] for v in vids[:30]]
        if len(last30) < 5:
            continue
        median = max(statistics.median(last30), 1)
        ch = channels.get(cid, {})
        age_days = (NOW - when(ch["created"])).days if ch.get("created") else None
        for v in vids:
            if not v["published"] or when(v["published"]) < cutoff:
                continue
            age = max((NOW - when(v["published"])).total_seconds() / 86400, 0.5)
            k = max(v["views"], 1) / 1000
            rows.append({**v, "channel_title": ch.get("title"), "channel_handle": ch.get("handle"),
                         "channel_subs": ch.get("subscribers"), "channel_age_days": age_days,
                         "niches": sorted(channels.get(cid, {}).get("niches", [])),
                         "channel_median_views": int(median),
                         "outlier": round(v["views"] / median, 2),
                         "views_per_day": int(v["views"] / age),
                         "likes_per_k": round(v["likes"] / k, 1),
                         "comments_per_k": round(v["comments"] / k, 2),
                         "new_channel": bool(age_days is not None and age_days < 183
                                             and (ch.get("subscribers") or 0) >= 10_000)})
    return rows


def pick_outliers(rows: list[dict], limit: int) -> list[dict]:
    """Every Short at >= 5x its channel, plus the top 10% by outlier score per niche."""
    keep = {r["id"]: r for r in rows if r["outlier"] >= 5}
    by_niche: dict[str, list] = {}
    for r in rows:
        for n in r["niches"] or ["?"]:
            by_niche.setdefault(n, []).append(r)
    for rs in by_niche.values():
        rs.sort(key=lambda r: r["outlier"], reverse=True)
        for r in rs[:max(5, len(rs) // 10)]:
            keep[r["id"]] = r
    out = sorted(keep.values(), key=lambda r: r["outlier"], reverse=True)
    return out[:limit]


def extras(outliers: list[dict], out: Path) -> None:
    """Cover image (public thumbnail) and auto-captions for each outlier; both best effort."""
    (out / "covers").mkdir(exist_ok=True)
    (out / "captions").mkdir(exist_ok=True)
    ytdlp = shutil.which("yt-dlp") or (shutil.which("yt-dlp", path=str(Path(sys.executable).parent)))
    if not ytdlp:
        log("yt-dlp not installed: skipping captions (covers still fetched)")
    fails = 0
    for i, r in enumerate(outliers, 1):
        vid = r["id"]
        try:
            with urllib.request.urlopen(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", timeout=20) as resp:
                (out / "covers" / f"{vid}.jpg").write_bytes(resp.read())
        except Exception:  # noqa: BLE001
            pass
        if ytdlp and fails < 15:
            p = subprocess.run([ytdlp, "--skip-download", "--write-auto-subs", "--write-subs",
                                "--sub-langs", "en.*,en", "--sub-format", "vtt", "--no-warnings", "-q",
                                "-o", str(out / "captions" / "%(id)s.%(ext)s"),
                                f"https://www.youtube.com/shorts/{vid}"],
                               capture_output=True, text=True, timeout=120)
            if p.returncode != 0:
                fails += 1
                if fails == 15:
                    log("captions keep failing (YouTube may block this server): stopped trying")
            time.sleep(1.5)
        if i % 25 == 0 or i == len(outliers):
            log(f"extras {i}/{len(outliers)}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path.home() / "meta-scan"))
    ap.add_argument("--budget", type=int, default=6000, help="max YouTube API units (search = 100)")
    ap.add_argument("--days", type=int, default=90, help="window for scored Shorts")
    ap.add_argument("--search-days", type=int, default=30, help="window for the discovery searches")
    ap.add_argument("--max-channels", type=int, default=250)
    ap.add_argument("--max-outliers", type=int, default=500)
    ap.add_argument("--seeds", default="", help="extra channel ids, comma separated")
    ap.add_argument("--channel", help="whose saved login to read with (default: first that has one)")
    ap.add_argument("--api-key", help="read with an API key instead of a channel login")
    ap.add_argument("--no-extras", action="store_true")
    a = ap.parse_args(argv)

    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    yt = service(a.channel, a.api_key)
    budget = Budget(a.budget)

    log("step 1/4: searching for the most-viewed Shorts of the last "
        f"{a.search_days} days (30 searches, 100 units each)")
    found, hits = discover(yt, budget, a.search_days)
    for cid in filter(None, (s.strip() for s in a.seeds.split(","))):
        found.setdefault(cid, {"niches": {"seed"}, "hits": 0, "best_rank": 99, "title": None})
    order = sorted(found, key=lambda c: (-found[c]["hits"], found[c]["best_rank"]))[:a.max_channels]
    log(f"step 1/4 done: {len(found)} channels found, scanning the top {len(order)}")

    log("step 2/4: reading each channel's Shorts")
    info = channel_info(yt, budget, order)
    for cid in order:
        info.setdefault(cid, {})["niches"] = found[cid]["niches"]
    shorts: dict[str, list] = {}
    for i, cid in enumerate(order, 1):
        shorts[cid] = channel_shorts(yt, budget, cid, a.days, pages=2)
        if i % 10 == 0 or i == len(order):
            log(f"step 2/4: {i}/{len(order)} channels, {sum(map(len, shorts.values()))} Shorts, "
                f"{budget.used} units used")
        if budget.used >= budget.limit:
            log("budget reached, stopping")
            break

    log("step 3/4: scoring")
    rows = score(info, shorts, a.days)
    outliers = pick_outliers(rows, a.max_outliers)
    log(f"step 3/4 done: {len(rows)} Shorts from the last {a.days} days, {len(outliers)} outliers kept")

    channels_out = {cid: {**v, "niches": sorted(v.get("niches", []))} for cid, v in info.items()}
    (out / "scan.json").write_text(json.dumps({
        "scanned_at": NOW.isoformat(), "units_used": budget.used, "days": a.days,
        "channels": channels_out, "search_hits": hits, "shorts": rows,
        "outlier_ids": [r["id"] for r in outliers]}, ensure_ascii=False), encoding="utf-8")

    if not a.no_extras:
        log(f"step 4/4: cover images and captions for {len(outliers)} outliers (takes a few minutes)")
        extras(outliers, out)

    tar = out.parent / "meta-scan.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        tf.add(out, arcname="meta-scan")
    log(f"done: {tar} ({tar.stat().st_size // 1024} KB), {budget.used} API units used")


if __name__ == "__main__":
    main()
