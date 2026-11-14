"""Read-only: what the dashboard's Make jobs are doing (Roeland 2026-11-14 "making for more than one
hour and nothing is done"). Prints every ~/meta-out/tclips/dashjobs job with its state and log tail,
the running processes, the transcript cache and what each channel has made. Changes nothing.

    python -m meta.tchanneljobs
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

OUT = Path.home() / "meta-out" / "tclips"


def main() -> None:
    from shorts.web.tfirst import job_state
    jobs = OUT / "dashjobs"
    print(f"== now {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC")
    for f in sorted(jobs.glob("*.json")) if jobs.is_dir() else []:
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"!! {f.name}: {e}")
            continue
        st = job_state(meta, jobs)
        ex = f.with_suffix(".exit")
        print(f"\n== job {f.stem}: {st} | {json.dumps(meta)} | exit={ex.read_text().strip() if ex.exists() else '-'}")
        lg = f.with_suffix(".log")
        if lg.exists():
            lines = lg.read_text(encoding="utf-8", errors="replace").splitlines()
            print(f"-- log {len(lines)} lines, {lg.stat().st_size} bytes, modified {time.strftime('%H:%M:%S', time.gmtime(lg.stat().st_mtime))} UTC")
            show = lines if len(lines) <= 120 else lines[:30] + ["  [...]"] + lines[-90:]
            for ln in show:
                print("   " + ln[:400])
    print("\n== processes")
    ps = subprocess.run(["ps", "-eo", "pid,ppid,etime,pcpu,pmem,args", "--sort=-pcpu"], capture_output=True, text=True).stdout
    for ln in ps.splitlines()[:1] + [l for l in ps.splitlines() if any(k in l for k in ("tclips", "whisper", "ffmpeg", "transcri", "yt-dlp", "python"))]:
        print("   " + ln[:300])
    print("\n== load", Path("/proc/loadavg").read_text().strip(), "| cpus", subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip())
    print(subprocess.run(["free", "-m"], capture_output=True, text=True).stdout)
    tx = Path.home() / "meta-out" / "transcripts"
    files = sorted(tx.glob("*.json"), key=lambda p: p.stat().st_mtime) if tx.is_dir() else []
    print(f"== transcripts: {len(files)}; newest:")
    for p in files[-12:]:
        try:
            n = len(json.loads(p.read_text(encoding="utf-8")).get("sentences") or [])
        except (OSError, ValueError):
            n = -1
        print(f"   {time.strftime('%H:%M', time.gmtime(p.stat().st_mtime))}  {n:4d} sentences  {p.name}")
    for d in sorted((OUT / "channels").glob("*")) if (OUT / "channels").is_dir() else []:
        made = d / "made.json"
        n = len(json.loads(made.read_text(encoding="utf-8"))) if made.exists() else 0
        print(f"== channel {d.name}: {n} in made.json, {len(list(d.glob('*.mp4')))} mp4")


if __name__ == "__main__":
    main()
