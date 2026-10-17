"""'python -m shorts bench': time the slow steps with each setting on your own footage, so a
speed change is measured instead of guessed. Writes only temporary files; changes nothing."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from . import media, render, scenes
from .config import Channel, Settings


def run(conn: sqlite3.Connection, settings: Settings, channel: Channel, n_renders: int = 2,
        n_videos: int = 2) -> None:
    print(f"NVENC (GPU encoding): {'yes' if media._nvenc_works() else 'no'}   "
          f"NVDEC (GPU decoding): {'yes' if media.nvdec_works() else 'no'}")
    bench_renders(conn, settings, channel, n_renders)
    bench_scenes(conn, settings, n_videos)


def bench_renders(conn: sqlite3.Connection, settings: Settings, channel: Channel, n: int) -> None:
    ids = [r[0] for r in conn.execute(
        """SELECT s.id FROM shorts s WHERE s.channel=? AND s.status IN ('rendered', 'approved', 'scheduled')
            AND NOT EXISTS (SELECT 1 FROM blocks b JOIN scenes sc ON sc.id=b.scene_id JOIN assets a ON a.id=sc.asset_id
                             WHERE b.short_id=s.id AND (a.path IS NULL OR a.status='cleaned'))
            ORDER BY s.id DESC LIMIT ?""", (channel.id, n))]
    if not ids:
        print("renders: no rendered Short with its footage still on disk to test with")
        return
    options = []
    if media._nvenc_works():
        options.append(("NVENC (GPU)", list(media.NVENC_ARGS)))
    options += [("x264 veryfast (new default)", media.video_encoder_args("libx264", "veryfast")),
                ("x264 medium (old)", media.video_encoder_args("libx264", "medium"))]
    print(f"\nrender: {len(ids)} Shorts per setting")
    for name, enc in options:
        secs, mb = 0.0, 0.0
        for sid in ids:
            work, args, total, _ = render.build_render(conn, settings, channel, sid, enc, out_name="bench.mp4")
            t0 = time.perf_counter()
            media.run(args, cwd=work)
            secs += time.perf_counter() - t0
            out = work / "bench.mp4"
            mb += out.stat().st_size / 1e6
            out.unlink()
        print(f"  {name:28} {secs / len(ids):6.1f} s per Short   {mb / len(ids):5.1f} MB")


def _starts(found: list[dict]) -> list[float]:
    return [f["start"] for f in found]


def bench_scenes(conn: sqlite3.Connection, settings: Settings, n: int) -> None:
    rows = [r for r in conn.execute(
        """SELECT * FROM assets WHERE status='scened' AND path IS NOT NULL AND duration > 20
            ORDER BY duration DESC LIMIT 50""").fetchall() if Path(r["path"]).exists()]
    if not rows:
        print("scenes: no cut video on disk to test with")
        return
    # A mix: the longest one (the old slow tail) and medium ones.
    picks = [rows[0]] + rows[len(rows) // 2:][: max(0, n - 1)]
    lengths = ", ".join(f"{(r['duration'] or 0) / 60:.1f} min" for r in picks)
    print(f"\nscene cutting: {len(picks)} videos ({lengths})")
    tmp = Path(tempfile.mkdtemp(prefix="bench-"))
    decoders = ["cpu"] + (["cuda"] if media.nvdec_works() else [])
    try:
        for r in picks:
            t0 = time.perf_counter()
            old = scenes.analyse_video(r["id"], r["path"], r["duration"], str(tmp / "old"))
            t_old = time.perf_counter() - t0
            line = f"  {r['source_id'][:40]:40} pyscenedetect {t_old:6.1f} s ({len(old)} scenes)"
            for dec in decoders:
                t0 = time.perf_counter()
                new = []
                for start, end in scenes.ranges_for(r["duration"] or 0):
                    new += scenes.analyse_range(r["id"], r["path"], start, min(end, r["duration"]),
                                                r["width"] or 0, r["height"] or 0, str(tmp / dec),
                                                settings.scene_fps, dec)
                t_new = time.perf_counter() - t0
                old_starts, new_starts = _starts(old), _starts(new)
                same = sum(1 for s in old_starts if any(abs(s - t) <= 0.35 for t in new_starts))
                agree = 100 * same / max(len(old_starts), 1)
                line += f" | fast/{dec} {t_new:6.1f} s ({len(new)} scenes, {agree:.0f}% same cuts)"
            print(line)
            t0 = time.perf_counter()
            from .dedupe import fingerprint
            fingerprint(r["path"])
            print(f"  {'':40} fingerprint {time.perf_counter() - t0:.1f} s")
        print("  (fast/... analyses a long video in one go here; in a real run its parts go to "
              "several workers at once)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
