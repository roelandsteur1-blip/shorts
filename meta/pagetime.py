"""Runner job "pagetime": how fast the dashboard pages are on the server, and where the time goes.

Read-only apart from what opening the pages does anyway. Three parts:
1. Real HTTP to the live dashboard (127.0.0.1:5000): every page 3 times, seconds and bytes.
2. The same pages in-process under cProfile (this checkout's code, the live data): the slowest
   functions per page.
3. Context: table sizes, database size, video file sizes, server load and running jobs.
Writes ~/meta-out/pagetime/pagetime.json (the runner copies it into the results repo)."""
from __future__ import annotations

import cProfile
import io
import json
import os
import pstats
import sqlite3
import time
import urllib.request
from pathlib import Path

from shorts.config import all_channels, load_settings

OUT = Path.home() / "meta-out" / "pagetime"
LIVE = "http://127.0.0.1:5000"


def pages(conn: sqlite3.Connection, chs) -> list[str]:
    out = ["/", "/review", "/schedule", "/stats", "/library", "/jobs", "/make", "/sources", "/music", "/voices"]
    if chs:
        out += [f"/channels/{chs[0].id}", f"/library/channel/{chs[0].id}", f"/schedule?channel={chs[0].id}"]
    r = conn.execute("SELECT id FROM shorts WHERE status='rendered' AND video_path IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if r:
        out.append(f"/short/{r[0]}")
    return out


def http(path: str) -> dict:
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(LIVE + path, timeout=120) as res:
            body = res.read()
            return {"s": round(time.perf_counter() - t, 3), "bytes": len(body), "status": res.status}
    except Exception as e:  # noqa: BLE001
        return {"s": round(time.perf_counter() - t, 3), "error": str(e)[:200]}


def profile(client, path: str) -> dict:
    pr = cProfile.Profile()
    t = time.perf_counter()
    pr.enable()
    res = client.get(path)
    pr.disable()
    took = time.perf_counter() - t
    buf = io.StringIO()
    st = pstats.Stats(pr, stream=buf)
    st.sort_stats("cumulative").print_stats(r"shorts|sqlite3|googleapiclient|httplib2|jinja2|ssl|socket", 30)
    st2 = io.StringIO()
    pstats.Stats(pr, stream=st2).sort_stats("tottime").print_stats(15)
    return {"s": round(took, 3), "status": res.status_code, "bytes": len(res.data),
            "cumulative": buf.getvalue()[-6000:], "tottime": st2.getvalue()[-3000:]}


def context(settings, conn: sqlite3.Connection) -> dict:
    tables = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        tables[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    sizes = []
    for (p,) in conn.execute("SELECT video_path FROM shorts WHERE video_path IS NOT NULL ORDER BY id DESC LIMIT 30"):
        try:
            sizes.append(os.path.getsize(p))
        except OSError:
            pass
    jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status IN ('running','queued')")]
    db = Path(settings.db_path)
    return {"tables": tables, "db_mb": round(db.stat().st_size / 1e6, 1),
            "wal_mb": round(Path(str(db) + "-wal").stat().st_size / 1e6, 1) if Path(str(db) + "-wal").exists() else 0,
            "video_mb_recent": [round(s / 1e6, 1) for s in sizes], "load": os.getloadavg(),
            "cpus": os.cpu_count(), "jobs": jobs}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings(Path(os.environ["META_CONFIG_DIR"]) if os.environ.get("META_CONFIG_DIR") else None)
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    chs = all_channels(settings.config_dir)
    result = {"context": context(settings, conn), "http": {}, "profile": {}}
    paths = pages(conn, chs)
    print("context:", json.dumps(result["context"])[:1500])
    for p in paths:
        result["http"][p] = [http(p) for _ in range(3)]
        print("http", p, result["http"][p])
    vid = next((p for p in paths if p.startswith("/short/")), None)
    if vid:
        result["http"]["/video" + vid[6:]] = [http("/video" + vid[6:])]
        print("video", result["http"]["/video" + vid[6:]])

    from shorts.web import create_app
    app = create_app(settings, start_runner=False)
    client = app.test_client()
    for p in paths:
        client.get(p)  # warm (imports, caches)
        result["profile"][p] = profile(client, p)
        print("profile", p, result["profile"][p]["s"], "s")
    # Home with the YouTube cache cleared: what the first visit after 10 minutes costs
    from shorts.web import home
    home._yt_cache.clear()
    result["profile"]["/ (YouTube cache empty)"] = profile(client, "/")
    print("profile / cold", result["profile"]["/ (YouTube cache empty)"]["s"], "s")
    (OUT / "pagetime.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    for p, r in result["profile"].items():
        print(f"\n===== {p}: {r['s']} s =====\n{r['cumulative'][-2500:]}")


if __name__ == "__main__":
    main()
