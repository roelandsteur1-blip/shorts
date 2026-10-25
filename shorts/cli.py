"""Command line: `python -m shorts <command>` (or `shorts <command>` after pip install -e .)."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import db
from .config import all_channels, list_channels, load_channel, load_settings


# Commands that keep the CPU busy for a while; they run at below-normal priority.
HEAVY = {"ingest", "restock", "categorize", "dedupe", "scenes", "embed", "make", "make-all", "finish",
         "rerender", "music"}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shorts", description="YouTube Shorts pipeline")
    p.add_argument("--channel", help="channel id (config/channels/<id>.toml)")
    p.add_argument("--data-dir", help="override data_dir from settings")
    p.add_argument("--config-dir", help="use another config folder (default: config/ in the repo)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="check that ffmpeg, Claude Code and folders are ready")

    pi = sub.add_parser("ingest", help="1. download videos for the channel's search terms (or restock its categories)")
    pi.add_argument("--per-term", type=int, default=5)
    pi.add_argument("--term", action="append", help="search term (repeatable); default: channel terms")
    pi.add_argument("--source", help="footage source (default: nasa); see 'python -m shorts sources'")

    prs = sub.add_parser("restock", help="1. download footage for the channel's categories that run low")
    prs.add_argument("--force", action="store_true", help="also when nothing is running low")

    pcat = sub.add_parser("categorize", help="1. sort new scenes into categories, and new videos per channel niche (Claude)")
    pcat.add_argument("--all", action="store_true", help="re-sort everything (after editing categories.toml)")

    psrc = sub.add_parser("sources", help="list footage sources, or try one: sources --try nasa \"rocket\"")
    psrc.add_argument("--try", dest="try_source", nargs=2, metavar=("SOURCE", "QUERY"),
                      help="search without downloading and show the rights decision per item (Try on the Sources page)")

    sub.add_parser("dedupe", help="1. find re-published copies already in the library (run once after updating)")
    sub.add_parser("scenes", help="1. cut downloaded videos into scenes and score them")
    pe = sub.add_parser("embed", help="1. make scenes searchable (CLIP, and the new search for new scenes)")
    pe.add_argument("--pe", action="store_true",
                    help="the new search's vectors for every scene that lacks them (once, ~a day: deploy/pe-embed.sh)")
    pe.add_argument("--pe-status", action="store_true", help="how much of the library the new search covers")
    pe.add_argument("--threads", type=int, help="with --pe: CPU threads for the model")
    pe.add_argument("--limit", type=int, help="with --pe: at most this many scenes")

    pm = sub.add_parser("make", help="2-5. hero shots -> scripts -> footage -> render")
    pm.add_argument("-n", "--count", type=int, default=5)
    pm.add_argument("--format", dest="format_id")
    pm.add_argument("--no-restock", action="store_true",
                    help="don't download new footage, even when the library is running low")

    pma = sub.add_parser("make-all", help="2-5. top every channel up to --to Shorts waiting, in one run")
    pma.add_argument("--to", type=int, default=2, help="Shorts waiting (to review or approved) per channel")
    pma.add_argument("--no-restock", action="store_true")

    pcl = sub.add_parser("cleanup", help="delete videos that are used up (all scenes used, Shorts uploaded)")
    pcl.add_argument("--dry-run", action="store_true", help="only show what would be deleted")

    pbk = sub.add_parser("backup", help="consistent copy of the database (default: data_dir/backups, keeps 7)")
    pbk.add_argument("--to", help="file to write instead")

    prr = sub.add_parser("rerender", help="render Shorts again (e.g. after adding music); keeps script and footage")
    prr.add_argument("ids", nargs="*", type=int, help="short ids; default: all waiting for review")

    pmu = sub.add_parser("music", help="background music: fetch CC0 tracks, tag moods, list them")
    pmu.add_argument("--want", type=int, help="fetch until the shared library has this many tracks "
                     "(default: music_min_tracks in settings)")
    pmu.add_argument("--retag", action="store_true", help="analyse every track again (mood, start)")
    pmu.add_argument("--list", action="store_true", help="only list the channel's tracks")
    pmu.add_argument("--claimed", type=int, metavar="TRACK_ID",
                     help="YouTube claimed this track: stop using it and re-render Shorts not uploaded yet")

    sub.add_parser("finish", help="4-5. resume rendering Shorts that stopped halfway")

    pd = sub.add_parser("dashboard", help="6. the dashboard on http://127.0.0.1:5000 (Tailscale makes it reachable)")
    pd.add_argument("--port", type=int, default=5000)

    pu = sub.add_parser("upload", help="7. upload approved Shorts, scheduled with publishAt")
    pu.add_argument("--limit", type=int)
    pu.add_argument("--no-schedule", action="store_true", help="upload as private without a publish time")

    sub.add_parser("upload-all", help="7. daily upload: every channel, up to channel_uploads_per_day each")

    pcn = sub.add_parser("connect", help="log in to YouTube for this channel and show which channel it is")
    pcn.add_argument("--fresh", action="store_true", help="forget the saved login first (other account)")

    pstat = sub.add_parser("stats", help="8. fetch analytics for uploaded Shorts")
    pstat.add_argument("--all", action="store_true", help="every channel with a YouTube login")

    pst = sub.add_parser("status", help="show counts per step")
    pst.add_argument("--per-video", action="store_true",
                     help="also list the videos that can still give Shorts, and how many")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # Hugging Face downloads log every HTTP request at INFO: that floods the job logs.
    for noisy in ("httpx", "httpcore", "huggingface_hub", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd in HEAVY:
        from .speed import lower_priority
        lower_priority()  # also when started from a terminal: the dashboard stays quick
    if args.config_dir:
        from . import config
        config.CONFIG_DIR = Path(args.config_dir).resolve()
    settings = load_settings(data_dir=args.data_dir)
    settings.ensure_dirs()
    from .server import load_env
    load_env(settings)
    channel = load_channel(args.channel or settings.default_channel)
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
        from .embed import embed_pending
        if args.all:
            resort(conn)
        # Scenes cut by a run that was stopped before embedding can't be sorted (or used) without it.
        if n := embed_pending(conn, settings):
            print(f"embedded {n} scenes")
        counts = sort_pending(conn, settings)
        print("sorted scenes: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "nothing new"))
        from .fit import sort_pending as sort_channels
        print(f"sorted {sort_channels(conn, settings)} videos per channel")
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
        from . import embed
        if args.pe_status:
            st = embed.pe_status(conn)
            print(f"new search: {st['ready']} of {st['scenes']} scenes ready ({st['share']:.0%} done, "
                  f"{st['unreadable']} unreadable); search uses it from {embed.PE_READY:.0%} on, "
                  f"now: {embed.search_space(conn, settings)}")
        elif args.pe:
            if n := embed.import_bench_pe(conn, settings):
                print(f"took {n} scenes' vectors from the clip-quality bench")
            n = embed.embed_pe(conn, settings, limit=args.limit, threads=args.threads)
            print(f"embedded {n} scenes for the new search")
        else:
            print(f"embedded {embed.embed_pending(conn, settings)} scenes")
    elif args.cmd == "make":
        from .pipeline import make
        done = make(conn, settings, channel, args.count, args.format_id, restock=not args.no_restock)
        print(f"{len(done)} Shorts ready for review on the dashboard")
    elif args.cmd == "make-all":
        from .pipeline import make_many, top_up_orders
        orders, enough = top_up_orders(conn, all_channels(settings.config_dir), args.to)
        if enough:
            print("already enough: " + ", ".join(enough))
        if orders:
            print("making " + ", ".join(f"{ch.id} {n}" for ch, n, _ in orders))
        for cid, n in make_many(conn, settings, orders, restock=not args.no_restock).items():
            print(f"{cid}: {n} Shorts ready for review")
    elif args.cmd == "cleanup":
        from .library import cleanup
        n, gb = cleanup(conn, settings, dry_run=args.dry_run)
        print(f"{'would free' if args.dry_run else 'freed'} {gb:.1f} GB ({n} videos)")
    elif args.cmd == "backup":
        from .server import backup
        print(f"backup: {backup(conn, settings, Path(args.to) if args.to else None)}")
    elif args.cmd == "music":
        return _music(conn, settings, channel, args)
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
    elif args.cmd == "dashboard":
        from .web import serve
        serve(settings, args.port)
    elif args.cmd == "upload":
        from .upload import upload_approved
        ids = upload_approved(conn, settings, channel, args.limit, schedule=not args.no_schedule)
        print(f"uploaded {len(ids)}: " + " ".join(f"https://youtu.be/{i}" for i in ids))
    elif args.cmd == "upload-all":
        from .upload import upload_all
        for cid, result in upload_all(conn, settings).items():
            print(f"{cid}: {result}")
    elif args.cmd == "connect":
        from .upload import connect
        found = connect(settings, channel, fresh=args.fresh)
        print(f"logged in: YouTube channel '{found['title']}' ({found['id']})")
        if channel.youtube_channel_id and channel.youtube_channel_id != found["id"]:
            print(f"!! this is not the channel confirmed for '{channel.id}' ({channel.youtube_channel_id})")
    elif args.cmd == "stats":
        if args.all:
            from .upload import fetch_stats_all
            for cid, result in fetch_stats_all(conn, settings).items():
                print(f"{cid}: {result}")
        else:
            from .upload import fetch_stats
            print(f"stats for {fetch_stats(conn, settings, channel)} live Shorts")
    elif args.cmd == "status":
        print(f"channels: {', '.join(list_channels())}")
        for label, sql in [
            ("videos", "SELECT status, COUNT(*) FROM assets GROUP BY status"),
            ("scenes", "SELECT CASE WHEN rejected THEN 'rejected' WHEN embedding IS NULL THEN 'not embedded' ELSE 'searchable' END, COUNT(*) FROM scenes GROUP BY 1"),
            ("shorts", "SELECT status, COUNT(*) FROM shorts WHERE channel=? GROUP BY status"),
        ]:
            params = (channel.id,) if "?" in sql else ()
            print(f"{label}: " + ", ".join(f"{k} {v}" for k, v in conn.execute(sql, params).fetchall()))
        from .hero import shorts_left
        free = shutil.disk_usage(settings.footage_dir).free / 1e9
        left, videos = shorts_left(conn, settings, channel.id, fits=channel)
        print(f"Shorts left in the library: {left} from {videos} videos "
              f"(restock below {settings.restock_below}); {free:.0f} GB free on {settings.footage_dir}")
        if channel.categories:
            print("per category: " + ", ".join(
                f"{c} {shorts_left(conn, settings, channel.id, [c], fits=channel)[0]}" for c in channel.categories))
        if args.per_video:
            from .hero import per_video
            for r in per_video(conn, settings, channel.id, fits=channel):
                print(f"  {r['left']} more (made {r['made']}, max {r['budget']}, "
                      f"{(r['duration'] or 0) / 60:.0f} min)  {r['title']}")
        unsorted = conn.execute("SELECT COUNT(*) FROM scenes WHERE rejected=0 AND sorted=0").fetchone()[0]
        if unsorted:
            print(f"{unsorted} scenes not sorted into categories yet (python -m shorts categorize)")
        from .fit import pending
        if waiting := len(pending(conn, [channel])):
            print(f"{waiting} videos not sorted for this channel yet (python -m shorts categorize)")
    return 0


def _music(conn, settings, channel, args) -> int:
    from . import music
    if args.claimed:
        from .render import render_short
        ids = music.claim(conn, settings, args.claimed)
        print(f"track {args.claimed} won't be used again; re-rendering {len(ids)} Shorts")
        for sid in ids:  # any channel: the library is shared
            cid = conn.execute("SELECT channel FROM shorts WHERE id=?", (sid,)).fetchone()[0]
            print(f"short {sid}: {render_short(conn, settings, load_channel(cid), sid)}")
        return 0
    if not args.list:
        music.sync(conn, settings)
        if args.retag:
            conn.execute("UPDATE tracks SET analysed=0 WHERE status='ok'")
        print(f"tagged {music.tag_pending(conn, settings)} tracks")
        want = args.want if args.want is not None else settings.music_min_tracks
        print(f"downloaded {music.fetch(conn, settings, channel, want)} tracks "
              f"({', '.join(music.music_sources(settings))})")
    info = music.summary(conn)
    print(f"{info['tracks']} usable tracks in {music.folder(settings)} (shared by all channels): "
          + ", ".join(f"{k} {v}" for k, v in sorted(info["moods"].items())))
    for t in music.usable(conn):
        print(f"  {t['id']:5} {t['mood'] or '-':10} start {t['start']:4.1f}s  {t['license']:14} {t['title'][:60]}")
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
        try:
            items = mod.search(query, page=1, session=s, key=key)
        except (requests.RequestException, ValueError) as e:
            print(f"search on {mod.LABEL} failed: {e}")
            return 1
        print(f"{len(items)} results for '{query}' on {mod.LABEL} (nothing is downloaded)")
        for item in items[:15]:
            try:
                item, r = check(item, s, key)
            except (requests.RequestException, ValueError) as e:
                print(f"- {item.title[:70]!r} [{item.source_id}]\n    details failed: {e}")
                continue
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
    line(shutil.which("ffmpeg") is not None, "ffmpeg", "sudo apt install ffmpeg")
    from importlib.util import find_spec
    line(find_spec("torch") is not None, "PyTorch", "run setup.sh")
    from .llm import find_claude
    claude = find_claude(settings.claude_cmd)
    line(claude is not None, "Claude Code CLI", "curl -fsSL https://claude.ai/install.sh | bash; add ~/.local/bin to PATH")
    if claude:
        v = subprocess.run([claude, "--version"], capture_output=True, text=True)
        print(f"     {v.stdout.strip()}")
    line(True, f"data folder {settings.data_dir}")
    free = shutil.disk_usage(settings.footage_dir).free / 1e9
    line(free >= settings.min_free_gb, f"footage folder {settings.footage_dir} ({free:.0f} GB free)",
         "free disk space or run 'python -m shorts cleanup'")
    from .music import MUSIC_EXT
    n = sum(1 for p in settings.music_dir.glob("*") if p.suffix.lower() in MUSIC_EXT)
    print(f"[--] music tracks: {n} in {settings.music_dir} (shared by all channels; fetched on the next Make)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
