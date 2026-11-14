"""Turn the classic pipeline off on the server, and measure what it leaves on disk (Roeland chose
"everything to transcript-first", 2026-11-14).

    python -m meta.classicoff            # pause every channel + disk report
    python -m meta.classicoff --report   # disk report only (changes nothing)

Pausing is the dashboard's own Pause button (shorts.channels.set_paused): one line `paused = true` in
each channel file. A paused channel is skipped by make-all and the daily upload. It does not touch
YouTube: what a channel already has scheduled still publishes, and the transcript-first upload to
historybuffed (meta.tclips upload) does not look at `paused`. Undo per channel: remove that line.

The disk report deletes nothing; it lists the biggest folders so Roeland can decide what goes.
Also prints the crontab, so a leftover classic cron line would show up.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from . import config_dir

HOME = Path.home()
OUT = HOME / "meta-out" / "classicoff"


def size_of(path: Path) -> int:
    """Bytes under path (du, so it is quick on big folders); 0 when it doesn't exist."""
    if not path.exists():
        return 0
    try:
        p = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True, timeout=900)
        return int(p.stdout.split()[0]) if p.stdout else 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def disk_report(settings) -> dict:
    roots = {"shorts-data": settings.data_dir, "footage": settings.footage_dir, "meta-out": HOME / "meta-out",
             "meta-results": HOME / "meta-results", "research-venv": HOME / "research-venv",
             "bench-venv": HOME / "bench-venv", "meta-scan": HOME / "meta-scan"}
    rows = []
    seen = set()
    for label, root in roots.items():
        root = Path(root)
        if not root.exists() or root.resolve() in seen:
            continue
        seen.add(root.resolve())
        subs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name) if root.is_dir() else []
        parts = [{"path": str(p), "bytes": size_of(p)} for p in subs]
        parts.sort(key=lambda r: -r["bytes"])
        rows.append({"label": label, "path": str(root), "bytes": size_of(root), "parts": parts[:25]})
    total, used, free = shutil.disk_usage(str(HOME))
    files = list(HOME.glob("meta-out/*.tar.gz")) + list(HOME.glob("*.tar.gz"))
    return {"disk": {"total": total, "used": used, "free": free}, "folders": rows,
            "tarballs": [{"path": str(f), "bytes": f.stat().st_size} for f in files]}


def pause_all(settings) -> list[str]:
    from shorts.channels import set_paused
    from shorts.config import all_channels
    done = []
    for ch in all_channels(settings.config_dir):
        if not ch.paused:
            set_paused(settings, ch.id, True)
            done.append(ch.id)
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="only the disk report, change nothing")
    args = ap.parse_args()
    from shorts.config import all_channels, load_settings
    settings = load_settings(config_dir())
    OUT.mkdir(parents=True, exist_ok=True)
    result: dict = {}
    if not args.report:
        paused = pause_all(settings)
        print(f"== paused {len(paused)} channels: {', '.join(paused) or '-'}")
        result["paused"] = paused
    print("== channels now:", ", ".join(f"{c.id}{' (paused)' if c.paused else ''}" for c in all_channels(settings.config_dir)))
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as e:
        cron = f"({e})"
    print("== crontab\n" + cron)
    rep = disk_report(settings)
    d = rep["disk"]
    print(f"== disk: {gb(d['used'])} used, {gb(d['free'])} free of {gb(d['total'])}")
    for r in rep["folders"]:
        print(f"-- {r['label']} {r['path']}: {gb(r['bytes'])}")
        for p in r["parts"]:
            if p["bytes"] > 50e6:
                print(f"     {gb(p['bytes']):>9}  {p['path']}")
    for t in rep["tarballs"]:
        print(f"   tarball {gb(t['bytes'])}  {t['path']}")
    result.update(rep, crontab=cron)
    (OUT / "classicoff.json").write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
