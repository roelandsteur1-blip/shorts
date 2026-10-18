"""Competitor research: learn how the most-viewed Shorts of 2-3 similar channels (the channel's
`inspiration` list) win reach, refreshed weekly: hook patterns, title patterns and format traits
(length, pacing, structure, ending). Never their topics: the channel's own niche alone decides what
a Short is about, so inspiration channels can be shared between channels without pulling them
off-niche or making them alike.

The YouTube Data API reads public uploads cheaply (playlistItems + videos: 1 quota unit per 50
videos), so this never eats into the upload budget. Claude turns the list into a pattern bank
that goes into the script prompt (shorts/script.py) only; hero picking and footage downloads
ignore it. It learns patterns only: their titles and clips are never copied."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from . import llm
from .config import Channel, Settings

log = logging.getLogger(__name__)

REFRESH_DAYS = 7
PAGES = 4               # x 50 recent Shorts per inspiration channel (1 quota unit each)
PER_CHANNEL = 15        # most-viewed Shorts per inspiration channel shown to Claude
MAX_SECONDS = 180       # Shorts can be up to 3 minutes
SUGGEST_COST = 100      # quota units of one search (suggest_channels)

_ID = re.compile(r"^UC[\w-]{22}$")


class ResearchError(RuntimeError):
    pass


# ---------- YouTube ----------

def reader(settings: Settings, channel: Channel):
    """A YouTube client for reading public data: an API key ('youtube' on the Sources page key
    store) when there is one, else this channel's login, else any other channel's login (reading
    public uploads needs no particular account)."""
    from googleapiclient.discovery import build
    from . import sources, upload
    from .config import load_channel

    if key := sources.api_key(settings, "youtube"):
        return build("youtube", "v3", developerKey=key, cache_discovery=False)
    ids = [channel.id] + sorted(p.stem.removeprefix("token-") for p in settings.secrets_dir.glob("token-*.json"))
    for cid in dict.fromkeys(ids):
        if not (settings.secrets_dir / f"token-{cid}.json").exists():
            continue
        try:
            ch = channel if cid == channel.id else load_channel(cid, settings.config_dir)
            return upload.youtube(settings, ch, interactive=False)
        except Exception as e:  # noqa: BLE001 - expired login or deleted channel: try the next one
            log.debug("no YouTube reader from %s: %s", cid, e)
    raise ResearchError("no YouTube login: use Connect YouTube on any channel's Settings page first")


def parse_ref(ref: str) -> tuple[str, str]:
    """('id', 'UC...') or ('handle', '@name') or ('user', 'name') from what was typed or pasted."""
    ref = ref.strip().rstrip("/")
    if m := re.search(r"/channel/(UC[\w-]{22})", ref):
        return "id", m.group(1)
    if m := re.search(r"/(@[\w.\-]+)", ref):
        return "handle", m.group(1)
    if m := re.search(r"/(?:user|c)/([\w.\-]+)", ref):
        return "user", m.group(1)
    if _ID.match(ref):
        return "id", ref
    name = ref.split("/")[-1]
    return "handle", name if name.startswith("@") else "@" + name


def resolve(yt, ref: str) -> dict:
    """{id, title} of a channel given as @handle, link or UC id."""
    kind, value = parse_ref(ref)
    args = {"id": value} if kind == "id" else {"forHandle": value} if kind == "handle" else {"forUsername": value}
    items = yt.channels().list(part="snippet", **args).execute().get("items") or []
    if not items and kind == "user":  # old /c/ links are usually the handle now
        items = yt.channels().list(part="snippet", forHandle="@" + value).execute().get("items") or []
    if not items:
        raise ResearchError(f"YouTube channel not found: {ref}")
    return {"id": items[0]["id"], "title": items[0]["snippet"].get("title") or value}


def seconds(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _playlist_ids(yt, playlist: str, pages: int) -> list[str]:
    from googleapiclient.errors import HttpError
    ids, token = [], None
    for _ in range(pages):
        try:
            res = yt.playlistItems().list(part="contentDetails", playlistId=playlist, maxResults=50,
                                          pageToken=token).execute()
        except HttpError as e:
            if getattr(e, "status_code", None) == 404 or getattr(getattr(e, "resp", None), "status", None) == 404:
                return ids
            raise
        ids += [it["contentDetails"]["videoId"] for it in res.get("items") or []]
        token = res.get("nextPageToken")
        if not token:
            break
    return ids


def recent_shorts(yt, channel_id: str, pages: int = PAGES) -> list[dict]:
    """The channel's recent Shorts with views. YouTube keeps a Shorts-only uploads playlist (UUSH...);
    when a channel has none, all uploads up to MAX_SECONDS long."""
    ids = _playlist_ids(yt, "UUSH" + channel_id[2:], pages) or _playlist_ids(yt, "UU" + channel_id[2:], pages)
    out = []
    for i in range(0, len(ids), 50):
        res = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]),
                               maxResults=50).execute()
        for v in res.get("items") or []:
            secs = seconds((v.get("contentDetails") or {}).get("duration", ""))
            if not 0 < secs <= MAX_SECONDS:
                continue
            st = v.get("statistics") or {}
            out.append({"video_id": v["id"], "title": (v.get("snippet") or {}).get("title") or "",
                        "published_at": (v.get("snippet") or {}).get("publishedAt"),
                        "views": int(st.get("viewCount") or 0), "likes": int(st.get("likeCount") or 0),
                        "comments": int(st.get("commentCount") or 0), "seconds": secs})
    return out


def suggest_channels(yt, channel: Channel, k: int = 8) -> list[dict]:
    """Channels whose Shorts get the most views for the niche (one search: SUGGEST_COST units).
    [{id, title, shorts, views}], most views first."""
    res = yt.search().list(part="snippet", q=channel.niche, type="video", videoDuration="short",
                           order="viewCount", maxResults=50, relevanceLanguage=channel.language).execute()
    items = res.get("items") or []
    vids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
    views = {}
    if vids:
        for v in yt.videos().list(part="statistics", id=",".join(vids)).execute().get("items") or []:
            views[v["id"]] = int((v.get("statistics") or {}).get("viewCount") or 0)
    found: dict[str, dict] = {}
    for it in items:
        sn, vid = it.get("snippet") or {}, it.get("id", {}).get("videoId")
        cid = sn.get("channelId")
        if not cid or cid == channel.youtube_channel_id:
            continue
        c = found.setdefault(cid, {"id": cid, "title": sn.get("channelTitle") or cid, "shorts": 0, "views": 0})
        c["shorts"] += 1
        c["views"] += views.get(vid, 0)
    return sorted(found.values(), key=lambda c: -c["views"])[:k]


# ---------- the pattern bank ----------

PROMPT = """You help the YouTube Shorts channel "{name}" learn how successful Shorts get reach.
Below are the most-viewed recent Shorts of {n} successful channels. Views include replays, so
compare them within one channel: "x median" says how far a Short beat that channel's usual Short.

Our Shorts are different from theirs: each one is built around real public-domain archive footage
({footage}), with {voice}, and teaches one true, sourced thing about our own niche ({niche}).
Their subjects don't matter to us and may be from another niche: our niche alone decides what our
Shorts are about. Learn only HOW they pull views: how hooks are built, how titles are built, and
format traits (length, pacing, structure, how they open and end, what makes people rewatch).
Write every pattern so it works for any subject, with [blanks] instead of their subjects.
Never copy a title, a hook or a clip.

Answer with only JSON:
{{"hooks": ["5-8 hook patterns that work here, as templates with [blanks]"],
  "titles": ["4-6 title patterns that work here, as templates with [blanks]"],
  "format": ["4-8 format traits that work here: length, pacing, structure, opening, ending, ..."],
  "avoid": ["2-4 things that do badly here"]}}

{videos}
"""


def _footage(channel: Channel) -> str:
    if not channel.categories or "space" in channel.categories:
        return "mostly NASA"
    return "newsreels, museum and government archives, nature and stock footage"


def _video_lines(conn: sqlite3.Connection, channel: str) -> tuple[str, int, int]:
    """Prompt text, Shorts shown, inspiration channels."""
    rows = conn.execute("""SELECT * FROM inspiration_videos WHERE channel=? ORDER BY views DESC""",
                        (channel,)).fetchall()
    by_src: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_src.setdefault(r["source_channel"], []).append(r)
    parts, shown = [], 0
    for src_rows in by_src.values():
        median = statistics.median(r["views"] for r in src_rows) or 1
        lines = [f'- {r["views"]:,} views ({r["views"] / median:.1f}x median), {r["seconds"]}s: "{r["title"]}"'
                 for r in src_rows[:PER_CHANNEL]]
        shown += len(lines)
        parts.append(f'Channel "{src_rows[0]["source_title"]}" ({len(src_rows)} recent Shorts, median '
                     f'{int(median):,} views):\n' + "\n".join(lines))
    return "\n\n".join(parts), shown, len(by_src)


def refresh(conn: sqlite3.Connection, settings: Settings, channel: Channel, yt=None) -> dict:
    """Fetch the inspiration channels' recent Shorts and have Claude build the pattern bank."""
    if not channel.inspiration:
        raise ResearchError(f"'{channel.id}' has no inspiration channels: add 2-3 on its Settings page")
    yt = yt or reader(settings, channel)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")  # also tells this run's rows apart
    got, failed = 0, []
    for ref in channel.inspiration:
        try:
            src = resolve(yt, ref)
            vids = recent_shorts(yt, src["id"])
        except Exception as e:  # noqa: BLE001 - one bad link must not stop the others
            log.warning("%s: inspiration channel %s skipped: %s", channel.id, ref, e)
            failed.append(ref)
            continue
        conn.execute("DELETE FROM inspiration_videos WHERE channel=? AND source_channel=?", (channel.id, src["id"]))
        conn.executemany(
            """INSERT OR REPLACE INTO inspiration_videos (channel, video_id, source_channel, source_title, title,
                   views, likes, comments, seconds, published_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(channel.id, v["video_id"], src["id"], src["title"], v["title"], v["views"], v["likes"],
              v["comments"], v["seconds"], v["published_at"], now) for v in vids])
        got += len(vids)
        log.info("%s: %d Shorts from %s", channel.id, len(vids), src["title"])
    if not got:
        conn.commit()
        raise ResearchError("no Shorts found on the inspiration channels" + (f" ({', '.join(failed)})" if failed else ""))
    # Channels taken off the list (or not reachable now) no longer count.
    conn.execute("DELETE FROM inspiration_videos WHERE channel=? AND fetched_at != ?", (channel.id, now))
    conn.commit()
    from .voice import channel_voice
    videos, shown, n = _video_lines(conn, channel.id)
    prompt = PROMPT.format(name=channel.name, niche=channel.niche, n=n, footage=_footage(channel),
                           voice="a voice-over reading the on-screen text" if channel_voice(channel)
                           else "text on screen and music (no voice-over)", videos=videos)
    data = llm.ask_json(prompt, settings.claude_cmd, conn=conn, stage="claude research")
    bank = clean_bank(data)
    if not (bank["hooks"] or bank["titles"]):
        raise ResearchError("Claude gave no hook or title patterns")
    conn.execute("INSERT OR REPLACE INTO idea_banks (channel, data, videos, made_at) VALUES (?,?,?,?)",
                 (channel.id, json.dumps(bank, ensure_ascii=False), shown, now))
    conn.commit()
    log.info("%s: pattern bank with %d patterns from %d Shorts on %d channels", channel.id, patterns(bank), shown, n)
    return {"patterns": patterns(bank), "shorts": shown, "channels": n, "failed": failed}


def clean_bank(data) -> dict:
    """Patterns only: a 'topics' list in the reply (or in a bank from before) is dropped."""
    data = data if isinstance(data, dict) else {}
    strs = lambda key, n: [" ".join(str(x).split())[:160] for x in data.get(key) or [] if str(x).strip()][:n]  # noqa: E731
    return {"hooks": strs("hooks", 8), "titles": strs("titles", 6), "format": strs("format", 8),
            "avoid": strs("avoid", 4)}


def patterns(b: dict) -> int:
    return sum(len(b.get(k) or []) for k in ("hooks", "titles", "format"))


def bank(conn: sqlite3.Connection, channel: str) -> dict | None:
    r = conn.execute("SELECT * FROM idea_banks WHERE channel=?", (channel,)).fetchone()
    if not r:
        return None
    return clean_bank(json.loads(r["data"])) | {"made_at": r["made_at"], "videos": r["videos"]}


def stale(conn: sqlite3.Connection, channel: Channel, days: int = REFRESH_DAYS) -> bool:
    """True when the channel has inspiration channels and no pattern bank (with format traits) younger than `days`."""
    if not channel.inspiration:
        return False
    r = conn.execute("SELECT made_at, data FROM idea_banks WHERE channel=?", (channel.id,)).fetchone()
    if not r or "format" not in json.loads(r[1] or "{}"):  # none yet, or an old bank with topics
        return True
    made = datetime.fromisoformat(r[0]).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - made > timedelta(days=days)


def refresh_stale(settings: Settings, channels: list[Channel]) -> dict[str, str]:
    """Best effort, for make and the daily run: refresh the banks older than a week. Never raises
    (no login, Claude limit, offline: the old bank, or none, is used)."""
    from . import db
    out = {}
    c = db.connect(settings.db_path)
    try:
        for ch in channels:
            if not stale(c, ch):
                continue
            try:
                r = refresh(c, settings, ch)
                out[ch.id] = f"{r['patterns']} patterns from {r['shorts']} Shorts"
            except Exception as e:  # noqa: BLE001
                log.warning("%s: competitor research not refreshed: %s", ch.id, e)
                out[ch.id] = f"not refreshed: {e}"
    finally:
        c.close()
    return out


# ---------- used by the script writer ----------

def prompt_note(conn: sqlite3.Connection, channel: str) -> str:
    """How to build the hook, title and pacing. Says nothing about what a Short is about."""
    b = bank(conn, channel)
    if not b or not patterns(b):
        return ""
    out = ("What gets reach on similar channels (from their most-viewed Shorts). Use these only for HOW "
           "you write the hook, title and pacing; the Short's subject still comes from the hero shot and "
           "this channel's niche alone. Never copy anyone's title or hook:\n")
    if b.get("hooks"):
        out += "Hook patterns: " + " | ".join(b["hooks"]) + "\n"
    if b.get("titles"):
        out += "Title patterns: " + " | ".join(b["titles"]) + "\n"
    if b.get("format"):
        out += "Format traits: " + "; ".join(b["format"]) + "\n"
    if b.get("avoid"):
        out += "Does badly: " + "; ".join(b["avoid"]) + "\n"
    return out + "\n"
