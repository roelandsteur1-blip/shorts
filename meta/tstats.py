"""YouTube numbers for the transcript-first channels (config/tchannels.toml) that are connected:
every video on the channel with live views and Analytics, into yt_videos (shorts/ytstats.py). The
dashboard's Stats page reads that table; its "Fetch now" button and a fetch every 6 hours run this.

    python -m meta.tstats
"""
from __future__ import annotations


def main() -> None:
    from shorts import db
    from shorts.config import load_channel, load_settings
    from shorts.server import load_env
    from shorts.upload import fetch_stats, token_path

    from . import config_dir
    from .tclips import load_tchannels
    settings = load_settings(config_dir())
    load_env(settings)
    conn = db.connect(settings.db_path)
    done = 0
    for c in load_tchannels():
        cid = c["id"]
        if not token_path(settings, cid).exists():
            print(f"{cid}: not connected to YouTube, skipped")
            continue
        try:
            n = fetch_stats(conn, settings, load_channel(cid, settings.config_dir), interactive=False)
            print(f"{cid}: {n} live Shorts")
            done += 1
        except Exception as e:  # noqa: BLE001 - one channel failing must not stop the rest (saved in yt_sync)
            print(f"{cid}: stats failed: {e}")
    print(f"== stats fetched for {done} channel(s)")


if __name__ == "__main__":
    main()
