"""Read-only: how each channel and each niche (topic set) performs on YouTube, from the Stats data.

    python -m meta.nichestats

Per channel: niche, video type, Shorts live, total/average/median views, engaged views, avg viewed %,
subscribers, and the average views at day 1/3/7 (stats_age, same age for every Short). Then the
same summed per niche, sorted by average views per Short. Never changes anything.
"""
from __future__ import annotations

import sqlite3
import statistics

from . import config_dir


def main() -> None:
    from shorts.config import all_channels, load_settings
    from shorts import metatype
    cdir = config_dir()
    settings = load_settings(cdir)
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    chans = {c.id: c for c in all_channels(cdir)}
    ids = sorted(set(chans) | {r[0] for r in conn.execute("SELECT DISTINCT channel FROM yt_videos")})
    niche = {cid: (metatype.niche_for(chans[cid]) if cid in chans else metatype.guess_niche(cid, cid)) or "?"
             for cid in ids}
    subs = {r["channel"]: r["subscribers"] for r in conn.execute("SELECT channel, subscribers FROM yt_sync")}

    def age_avg(cid: str, days: int) -> float | None:
        r = conn.execute("""SELECT AVG(a.views) v, COUNT(*) n FROM stats_age a JOIN shorts s ON s.id=a.short_id
                            WHERE s.channel=? AND a.days=?""", (cid, days)).fetchone()
        return r["v"] if r["n"] else None

    rows = []
    for cid in ids:
        vids = conn.execute("""SELECT views, engaged_views, avg_view_pct, short_id FROM yt_videos
                               WHERE channel=? AND state='live' AND is_short=1""", (cid,)).fetchall()
        views = [v["views"] or 0 for v in vids]
        pct = [v["avg_view_pct"] for v in vids if v["avg_view_pct"] is not None]
        rows.append({
            "channel": cid, "niche": niche[cid],
            "type": getattr(chans.get(cid), "video_type", "-"),
            "live": len(views), "pipeline": sum(1 for v in vids if v["short_id"]),
            "views": sum(views), "avg": (sum(views) / len(views)) if views else 0,
            "median": statistics.median(views) if views else 0,
            "engaged": sum(v["engaged_views"] or 0 for v in vids),
            "pct": (sum(pct) / len(pct)) if pct else None,
            "subs": subs.get(cid),
            "d1": age_avg(cid, 1), "d3": age_avg(cid, 3), "d7": age_avg(cid, 7),
        })

    f = lambda x: "-" if x is None else (f"{x:.0f}" if isinstance(x, float) else str(x))  # noqa: E731
    print("== per channel (live Shorts; views = live Data API views; d1/d3/d7 = avg Analytics views at that age)")
    print("channel | niche | type | live | pipeline | views | avg | median | engaged | avg viewed % | subs | d1 | d3 | d7")
    for r in sorted(rows, key=lambda r: -r["avg"]):
        print(" | ".join(f(r[k]) for k in ("channel", "niche", "type", "live", "pipeline", "views", "avg", "median",
                                            "engaged", "pct", "subs", "d1", "d3", "d7")))
    print("\n== per niche (summed over its channels), best average first")
    print("niche | channels | live | views | avg per Short | subs | quiz+ranking possible")
    per = {}
    for r in rows:
        p = per.setdefault(r["niche"], {"ch": [], "live": 0, "views": 0, "subs": 0})
        p["ch"].append(r["channel"])
        p["live"] += r["live"]
        p["views"] += r["views"]
        p["subs"] += r["subs"] or 0
    for n, p in sorted(per.items(), key=lambda kv: -(kv[1]["views"] / kv[1]["live"] if kv[1]["live"] else 0)):
        avg = p["views"] / p["live"] if p["live"] else 0
        quiz = "yes" if "quiz" in metatype.available(n if n != "?" else None) else "no"
        print(f"{n} | {','.join(p['ch'])} | {p['live']} | {p['views']} | {avg:.0f} | {p['subs']} | {quiz}")


if __name__ == "__main__":
    main()
