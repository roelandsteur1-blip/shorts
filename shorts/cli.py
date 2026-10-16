"""Command line: `python -m shorts <command>` (or `shorts <command>` after pip install -e .)."""
from __future__ import annotations

import argparse
import logging
import sys

from . import db
from .config import list_channels, load_channel, load_settings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shorts", description="YouTube Shorts pipeline")
    p.add_argument("--channel", help="channel id (config/channels/<id>.toml)")
    p.add_argument("--data-dir", help="override data_dir from settings")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="check that ffmpeg, GPU, Claude Code and folders are ready")

    pi = sub.add_parser("ingest", help="1. download NASA videos for the channel's search terms")
    pi.add_argument("--per-term", type=int, default=5)
    pi.add_argument("--term", action="append", help="search term (repeatable); default: channel terms")

    sub.add_parser("dedupe", help="1. find re-published copies already in the library (run once after updating)")
    sub.add_parser("scenes", help="1. cut downloaded videos into scenes and score them")
    sub.add_parser("embed", help="1. make scenes searchable (CLIP on the GPU)")

    ps = sub.add_parser("search", help="try the footage search: shorts search \"saturn rings\"")
    ps.add_argument("query")
    ps.add_argument("-k", type=int, default=8)

    pm = sub.add_parser("make", help="2-5. hero shots -> scripts -> fact check -> footage -> render")
    pm.add_argument("-n", "--count", type=int, default=5)
    pm.add_argument("--format", dest="format_id")

    prr = sub.add_parser("rerender", help="render Shorts again (e.g. after adding music); keeps script and footage")
    prr.add_argument("ids", nargs="*", type=int, help="short ids; default: all waiting for review")

    sub.add_parser("finish", help="4-5. resume fact check/render for Shorts that stopped halfway")

    pr = sub.add_parser("review", help="6. open the review page")
    pr.add_argument("--port", type=int, default=5000)

    pu = sub.add_parser("upload", help="7. upload approved Shorts, scheduled with publishAt")
    pu.add_argument("--limit", type=int)
    pu.add_argument("--no-schedule", action="store_true", help="upload as private without a publish time")

    sub.add_parser("stats", help="8. fetch analytics for uploaded Shorts")
    sub.add_parser("status", help="show counts per step")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    settings = load_settings(data_dir=args.data_dir)
    settings.ensure_dirs()
    channel = load_channel(args.channel or settings.default_channel)
    settings.ensure_channel_dirs([channel.music_folder or channel.id])
    conn = db.connect(settings.db_path)

    if args.cmd == "check":
        return _check(settings)
    if args.cmd == "ingest":
        from .ingest_nasa import ingest
        n = ingest(conn, settings, args.term or channel.search_terms, per_term=args.per_term)
        print(f"downloaded {n} new videos")
    elif args.cmd == "dedupe":
        from .dedupe import dedupe_library
        res = dedupe_library(conn)
        print(f"fingerprinted {res['fingerprinted']} videos; {res['duplicate_videos']} duplicate videos "
              f"and {res['duplicate_scenes']} duplicate scenes taken out of use")
    elif args.cmd == "scenes":
        from .scenes import process_all
        print(f"{process_all(conn, settings)} scenes")
    elif args.cmd == "embed":
        from .embed import embed_pending
        print(f"embedded {embed_pending(conn, settings)} scenes")
    elif args.cmd == "search":
        from .embed import search
        for sid, score in search(conn, settings, args.query, k=args.k):
            r = conn.execute("""SELECT sc.keyframe, a.title FROM scenes sc JOIN assets a ON a.id=sc.asset_id
                                 WHERE sc.id=?""", (sid,)).fetchone()
            print(f"{score:.3f}  scene {sid}  {r['title']}  {r['keyframe']}")
    elif args.cmd == "make":
        from .pipeline import make
        done = make(conn, settings, channel, args.count, args.format_id)
        print(f"{len(done)} Shorts ready for review: run 'python -m shorts review'")
    elif args.cmd == "finish":
        from .pipeline import finish
        finish(conn, settings, channel)
    elif args.cmd == "rerender":
        from .render import render_short
        ids = args.ids or [r[0] for r in conn.execute(
            "SELECT id FROM shorts WHERE channel=? AND status='rendered' ORDER BY id", (channel.id,))]
        for sid in ids:
            print(f"short {sid}: {render_short(conn, settings, channel, sid)}")
    elif args.cmd == "review":
        from .review import serve
        serve(settings, args.port)
    elif args.cmd == "upload":
        from .upload import upload_approved
        ids = upload_approved(conn, settings, channel, args.limit, schedule=not args.no_schedule)
        print(f"uploaded {len(ids)}: " + " ".join(f"https://youtu.be/{i}" for i in ids))
    elif args.cmd == "stats":
        from .upload import fetch_stats
        print(f"stats for {fetch_stats(conn, settings, channel)} Shorts")
    elif args.cmd == "status":
        print(f"channels: {', '.join(list_channels())}")
        for label, sql in [
            ("videos", "SELECT status, COUNT(*) FROM assets GROUP BY status"),
            ("scenes", "SELECT CASE WHEN rejected THEN 'rejected' WHEN embedding IS NULL THEN 'not embedded' ELSE 'searchable' END, COUNT(*) FROM scenes GROUP BY 1"),
            ("shorts", f"SELECT status, COUNT(*) FROM shorts WHERE channel='{channel.id}' GROUP BY status"),
        ]:
            print(f"{label}: " + ", ".join(f"{k} {v}" for k, v in conn.execute(sql).fetchall()))
    return 0


def _check(settings) -> int:
    import shutil
    import subprocess

    ok = True

    def line(good: bool, what: str, hint: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"[{'OK' if good else '!!'}] {what}" + (f"  -> {hint}" if not good and hint else ""))

    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "install Python 3.11+")
    line(shutil.which("ffmpeg") is not None, "ffmpeg", "winget install Gyan.FFmpeg, then open a new terminal")
    if shutil.which("ffmpeg"):
        from .media import _nvenc_works
        line(_nvenc_works(), "NVENC (GPU video encoding)", "update the NVIDIA driver; falls back to CPU encoding")
    try:
        import torch
        line(torch.cuda.is_available(), f"PyTorch CUDA ({torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no GPU'})",
             "re-run setup.ps1 (installs the CUDA build of PyTorch)")
    except ImportError:
        line(False, "PyTorch", "run setup.ps1")
    from .llm import find_claude
    claude = find_claude(settings.claude_cmd)
    line(claude is not None, "Claude Code CLI", "irm https://claude.ai/install.ps1 | iex, then run 'claude' once to log in")
    if claude:
        v = subprocess.run([claude, "--version"], capture_output=True, text=True)
        print(f"     {v.stdout.strip()}")
    line(True, f"data folder {settings.data_dir}")
    if "onedrive" in str(settings.data_dir).lower():
        line(False, "data folder outside OneDrive", "set data_dir in config/local.toml")
    from .render import MUSIC_EXT
    for folder in sorted(p for p in settings.music_dir.iterdir() if p.is_dir()):
        n = sum(1 for p in folder.glob("*") if p.suffix.lower() in MUSIC_EXT)
        print(f"[--] music tracks: {n} in {folder} (optional)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
