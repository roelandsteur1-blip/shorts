"""Doubled uploads on the transcript-first channels (Roeland 2026-11-18 "0 views across the board").

Until 2026-11-18 the hourly upload round uploaded a Short again once it had gone public (meta/tupload.py
done_paths missed the 'published' status), so each channel published the same two Shorts over and over.
The first copy keeps its views; every later copy of the same video file is a double.

    python -m meta.tdupes             # list the doubles per channel (read-only), ~/meta-out/tdupes/tdupes.json
    python -m meta.tdupes --delete    # also delete the doubles on YouTube and mark their rows rejected

The first copy (earliest publish time) of every video file is never touched.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FORMAT_ID = "tclips"
OUT = Path.home() / "meta-out" / "tdupes"


def doubles(conn, cid: str) -> list[dict]:
    """Later copies of a video file that already went up on this channel, oldest first."""
    rows = [dict(r) for r in conn.execute(
        """SELECT id, youtube_id, title, status, publish_at, video_path FROM shorts
           WHERE channel=? AND format_id=? AND youtube_id IS NOT NULL AND status NOT IN ('failed', 'rejected')
           ORDER BY publish_at, id""", (cid, FORMAT_ID))]
    seen, out = {}, []
    for r in rows:
        if r["video_path"] in seen:
            out.append({**r, "first": seen[r["video_path"]]})
        else:
            seen[r["video_path"]] = r["youtube_id"]
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true")
    a = ap.parse_args(argv)
    from shorts import upload as up
    from shorts.config import load_channel

    from .common import open_library
    from .tclips import load_tchannels
    from .tupload import connected
    settings, conn, _llm = open_library()
    report = {}
    for c in load_tchannels():
        cid = c["id"]
        found = doubles(conn, cid)
        firsts = {r[0]: r[1] for r in conn.execute(
            "SELECT video_path, COUNT(*) FROM shorts WHERE channel=? AND format_id=? AND youtube_id IS NOT NULL "
            "AND status NOT IN ('failed', 'rejected') GROUP BY video_path", (cid, FORMAT_ID))}
        print(f"[dupes] {cid}: {len(firsts)} different Shorts uploaded, {len(found)} doubles", flush=True)
        for d in found:
            print(f"[dupes]   {d['youtube_id']} ({d['status']}, {d['publish_at']}) copy of {d['first']}: {d['title']}",
                  flush=True)
        report[cid] = {"different": len(firsts), "doubles": found, "deleted": []}
        if not a.delete or not found:
            continue
        if not connected(settings, cid):
            print(f"[dupes] {cid}: not connected to YouTube, nothing deleted", flush=True)
            continue
        ch = load_channel(cid, settings.config_dir)
        if not up.can_manage(settings, ch):
            print(f"[dupes] {cid}: the saved login may not delete videos, nothing deleted", flush=True)
            continue
        yt = up.youtube(settings, ch, interactive=False)
        up.check_channel(yt, ch)
        for d in found:
            try:
                yt.videos().delete(id=d["youtube_id"]).execute()
            except Exception as e:  # noqa: BLE001
                print(f"[dupes] {cid}: {d['youtube_id']} not deleted: {e}", flush=True)
                continue
            conn.execute("UPDATE shorts SET status='rejected', reject_reason=?, updated_at=datetime('now') WHERE id=?",
                         (f"double upload of {d['first']} (deleted 2026-11-18)", d["id"]))
            conn.commit()
            report[cid]["deleted"].append(d["youtube_id"])
            print(f"[dupes] {cid}: deleted {d['youtube_id']}: {d['title']}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tdupes.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
