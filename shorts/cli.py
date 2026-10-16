"""Command line: `python -m shorts <command>` (or `shorts <command>` after pip install -e .)."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import db
from .config import list_channels, load_channel, load_settings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shorts", description="YouTube Shorts pipeline")
    p.add_argument("--channel", help="channel id (config/channels/<id>.toml)")
    p.add_argument("--data-dir", help="override data_dir from settings")
    p.add_argument("--config-dir", help="use another config folder (default: config/ in the repo)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="check that ffmpeg, GPU, Claude Code and folders are ready")

    pi = sub.add_parser("ingest", help="1. download videos for the channel's search terms (or restock its categories)")
    pi.add_argument("--per-term", type=int, default=5)
    pi.add_argument("--term", action="append", help="search term (repeatable); default: channel terms")
    pi.add_argument("--source", help="footage source (default: nasa); see 'python -m shorts sources'")

    prs = sub.add_parser("restock", help="1. download footage for the channel's categories that run low")
    prs.add_argument("--force", action="store_true", help="also when nothing is running low")

    pcat = sub.add_parser("categorize", help="1. sort new scenes into categories (metadata, CLIP, Claude)")
    pcat.add_argument("--all", action="store_true", help="re-sort everything (keeps hand-sorted scenes)")

    psrc = sub.add_parser("sources", help="list footage sources, or try one: sources --try loc \"rocket\"")
    psrc.add_argument("--try", dest="try_source", nargs=2, metavar=("SOURCE", "QUERY"),
                      help="search without downloading and show the rights decision per item")

    sub.add_parser("dedupe", help="1. find re-published copies already in the library (run once after updating)")
    sub.add_parser("scenes", help="1. cut downloaded videos into scenes and score them")
    sub.add_parser("embed", help="1. make scenes searchable (CLIP on the GPU)")

    ps = sub.add_parser("search", help="try the footage search: shorts search \"saturn rings\"")
    ps.add_argument("query")
    ps.add_argument("-k", type=int, default=8)

    pm = sub.add_parser("make", help="2-5. hero shots -> scripts -> fact check -> footage -> render")
    pm.add_argument("-n", "--count", type=int, default=5)
    pm.add_argument("--format", dest="format_id")
    pm.add_argument("--no-restock", action="store_true",
                    help="don't download new footage, even when the library is running low")

    pcl = sub.add_parser("cleanup", help="delete videos that are used up (all scenes used, Shorts uploaded)")
    pcl.add_argument("--dry-run", action="store_true", help="only show what would be deleted")
    sub.add_parser("move-footage", help="move downloaded videos to footage_dir (after changing it)")

    prr = sub.add_parser("rerender", help="render Shorts again (e.g. after adding music); keeps script and footage")
    prr.add_argument("ids", nargs="*", type=int, help="short ids; default: all waiting for review")

    sub.add_parser("finish", help="4-5. resume fact check/render for Shorts that stopped halfway")

    for name, text in (("dashboard", "open the dashboard on http://127.0.0.1:5000 (this laptop only)"),
                       ("review", "6. same as 'dashboard'")):
        pr = sub.add_parser(name, help=text)
        pr.add_argument("--port", type=int, default=5000)

    pu = sub.add_parser("upload", help="7. upload approved Shorts, scheduled with publishAt")
    pu.add_argument("--limit", type=int)
    pu.add_argument("--no-schedule", action="store_true", help="upload as private without a publish time")

    pcn = sub.add_parser("connect", help="log in to YouTube for this channel and show which channel it is")
    pcn.add_argument("--fresh", action="store_true", help="forget the saved login first (other account)")

    sub.add_parser("stats", help="8. fetch analytics for uploaded Shorts")
    sub.add_parser("status", help="show counts per step")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.config_dir:
        from . import config
        config.CONFIG_DIR = Path(args.config_dir).resolve()
    settings = load_settings(data_dir=args.data_dir)
    settings.ensure_dirs()
    channel = load_channel(args.channel or settings.default_channel)
    settings.ensure_channel_dirs([channel.music_folder or channel.id])
    conn = db.connect(settings.db_path)

    if args.cmd == "check":
        return _check(settings)
    if args.cmd == "ingest":
        from .sources.common import ingest
        n = ingest(conn, settings, args.source or "nasa", args.term or channel.search_terms, per_term=args.per_term)
        print(f"downloaded {n} new videos")
    elif args.cmd == "restock":
        from .library import restock
        need = 10 ** 6 if args.force else 0
        print(f"downloaded {restock(conn, settings, channel, need=need)} new videos")
    elif args.cmd == "categorize":
        from .categorize import resort, sort_pending
        if args.all:
            resort(conn)
        counts = sort_pending(conn, settings)
        print("sorted scenes: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "nothing new"))
    elif args.cmd == "sources":
        return _sources(conn, settings, args.try_source)
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
        done = make(conn, settings, channel, args.count, args.format_id, restock=not args.no_restock)
        print(f"{len(done)} Shorts ready for review: run 'python -m shorts review'")
    elif args.cmd == "cleanup":
        from .library import cleanup
        n, gb = cleanup(conn, dry_run=args.dry_run)
        print(f"{'would free' if args.dry_run else 'freed'} {gb:.1f} GB ({n} videos)")
    elif args.cmd == "move-footage":
        from .library import move_footage
        n, gb = move_footage(conn, settings)
        print(f"moved {n} videos ({gb:.1f} GB) to {settings.footage_dir}")
    elif args.cmd == "finish":
        from .pipeline import finish
        finish(conn, settings, channel)
    elif args.cmd == "rerender":
        from .render import render_short
        ids = args.ids or [r[0] for r in conn.execute(
            "SELECT id FROM shorts WHERE channel=? AND status='rendered' ORDER BY id", (channel.id,))]
        for sid in ids:
            row = conn.execute("SELECT status, youtube_id FROM shorts WHERE id=?", (sid,)).fetchone()
            if row is None or row["youtube_id"] or row["status"] not in ("rendered", "approved", "failed"):
                print(f"short {sid}: skipped (status {row['status'] if row else 'unknown'}; "
                      "only Shorts that are not uploaded yet can be re-rendered)")
                continue
            print(f"short {sid}: {render_short(conn, settings, channel, sid)}")
    elif args.cmd in ("dashboard", "review"):
        from .web import serve
        serve(settings, args.port)
    elif args.cmd == "upload":
        from .upload import upload_approved
        ids = upload_approved(conn, settings, channel, args.limit, schedule=not args.no_schedule)
        print(f"uploaded {len(ids)}: " + " ".join(f"https://youtu.be/{i}" for i in ids))
    elif args.cmd == "connect":
        from .upload import connect
        found = connect(settings, channel, fresh=args.fresh)
        print(f"logged in: YouTube channel '{found['title']}' ({found['id']})")
        if channel.youtube_channel_id and channel.youtube_channel_id != found["id"]:
            print(f"!! this is not the channel confirmed for '{channel.id}' ({channel.youtube_channel_id})")
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
        from .library import unused_hero_videos
        free = shutil.disk_usage(settings.footage_dir).free / 1e9
        print(f"unused hero videos: {unused_hero_videos(conn, settings, channel)} "
              f"(restock below {settings.restock_below}); {free:.0f} GB free on {settings.footage_dir}")
        if channel.categories:
            print("per category: " + ", ".join(
                f"{c} {unused_hero_videos(conn, settings, channel, c)}" for c in channel.categories))
        unsorted = conn.execute("SELECT COUNT(*) FROM scenes WHERE rejected=0 AND sorted=0").fetchone()[0]
        if unsorted:
            print(f"{unsorted} scenes not sorted into categories yet (python -m shorts categorize)")
    return 0


def _sources(conn, settings, try_source) -> int:
    from . import sources
    if try_source:
        import requests
        from .sources.common import check
        name, query = try_source
        mod = sources.get(name)
        s = requests.Session()
        key = sources.api_key(settings, name)
        items = mod.search(query, page=1, session=s, key=key)
        print(f"{len(items)} results for '{query}' on {mod.LABEL} (nothing is downloaded)")
        for item in items[:15]:
            item, r = check(item, s, key)
            verdict = f"REJECT {r.reject}" if r.reject else f"OK {r.license}" + (" (rights check)" if r.needs_check else "")
            print(f"- {item.title[:70]!r} [{item.source_id}]\n    {verdict}"
                  + (f"\n    credit: {r.attribution}" if r.attribution else "")
                  + (f"\n    file: {item.file_url}" if item.file_url else ""))
        return 0
    for mod in sources.all_sources():
        ok, why = sources.usable(settings, mod.NAME)
        n = dict(conn.execute("SELECT status, COUNT(*) FROM assets WHERE source=? GROUP BY status",
                              (mod.NAME,)).fetchall())
        print(f"{mod.NAME:17} {'on ' if ok else 'off'} {mod.LABEL}: {mod.LICENSE}"
              + (f"  [{why}]" if why else "")
              + f"\n{'':21}downloaded {sum(v for k, v in n.items() if k not in ('rejected', 'failed', 'new', 'duplicate'))}, "
                f"rejected by rights check {n.get('rejected', 0)}")
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
    if "onedrive" in str(settings.data_dir).lower() or "onedrive" in str(settings.footage_dir).lower():
        line(False, "data folder outside OneDrive", "set data_dir / footage_dir in config/local.toml")
    free = shutil.disk_usage(settings.footage_dir).free / 1e9
    line(free >= settings.min_free_gb, f"footage folder {settings.footage_dir} ({free:.0f} GB free)",
         "free disk space or run 'python -m shorts cleanup'")
    from .render import MUSIC_EXT
    for folder in sorted(p for p in settings.music_dir.iterdir() if p.is_dir()):
        n = sum(1 for p in folder.glob("*") if p.suffix.lower() in MUSIC_EXT)
        print(f"[--] music tracks: {n} in {folder} (optional)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
