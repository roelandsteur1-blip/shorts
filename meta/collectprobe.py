"""Read-only look at why the footage collector finds nothing for a subject.

    python -m meta.collectprobe

Runs the live collector (shorts/collector.py) for the subjects in SUBJECTS on their channel, the
same way a make does, but never adds anything to the library (add_to_library is replaced by a
stub) and opens the database read-only. Prints, per subject: what each archive search returned,
which pieces could be fetched (and why not), the gate faults, and every judge verdict (shows,
confidence, era_ok, same_shot, interest, why), plus whether the clip would pass without the
old-film rule. Writes ~/meta-out/collectprobe/probe.json. Costs a few Claude calls per subject.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from . import config_dir

# (niche, subject, show): job 123 (8 Nov, after the old-film fix) found nothing for these.
SUBJECTS = [
    ("retrotech", "Handcar", None),
    ("retrotech", "Polaroid Land Camera", None),
    ("retrotech", "Movie Camera Newman Sinclair", None),
    ("retrotech", "Leica I", None),
]
OUT = Path.home() / "meta-out" / "collectprobe"


def _channel(niche: str):
    """A text card channel of this niche (the job 120 one), else any channel of it."""
    from shorts import metatype
    from shorts.config import all_channels
    chans = [c for c in all_channels(config_dir()) if metatype.niche_for(c) == niche]
    chans.sort(key=lambda c: (getattr(c, "video_type", "") != "textcard", c.id))
    if not chans:
        raise SystemExit(f"no channel with niche {niche}")
    return chans[0]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from shorts import collector, judge
    from shorts.config import load_settings
    from meta.collector import pieces

    import os
    from shorts.server import load_env
    settings = load_settings(config_dir())
    load_env(settings)  # the Claude login, as `python -m shorts` on the server
    local_bin = str(Path.home() / ".local" / "bin")  # cron's PATH has no `claude`
    os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    print(f"footage_judge={settings.footage_judge!r} -> mode {judge.mode(settings)!r}, "
          f"MIN_INTEREST={judge.MIN_INTEREST}, MIN_CONFIDENCE={judge.MIN_CONFIDENCE}, "
          f"OLD_NICHES={sorted(judge.OLD_NICHES)}", flush=True)

    seen: dict = {}
    real_cands, real_judge = pieces.candidates, judge.judge

    def cands(subject, lib, scorer, keys, sources=None):
        out = real_cands(subject, lib, scorer, keys, sources=sources)
        seen.setdefault("candidates", []).extend(
            {k: c.get(k) for k in ("source", "kind", "title", "thumb_score", "licence", "error")} for c in out)
        return out

    def judged(st, clips, name, niche, show=""):
        v = real_judge(st, clips, name, niche, show)
        for c in clips:
            x = v.get(c["label"])
            row = {"label": c["label"], "path": Path(c["path"]).name, "verdict": x}
            if x:
                row["passes_now"] = judge.passes(x) and x["interest"] >= judge.MIN_INTEREST
                row["passes_without_era"] = (x["shows"] and x["confidence"] >= judge.MIN_CONFIDENCE
                                             and x["same_shot"] and x["interest"] >= judge.MIN_INTEREST)
            seen.setdefault("verdicts", []).append(row)
        return v

    def stub_add(conn_, settings_, channel_, c, subject):
        seen["picked"] = {k: c.get(k) for k in ("source", "kind", "title", "label")}
        return -1

    pieces.candidates, judge.judge, collector.add_to_library = cands, judged, stub_add
    report = []
    for cid, name, show in SUBJECTS:
        seen.clear()
        channel = _channel(cid)
        col = collector.Collector(conn, settings, channel)
        print(f"\n=== {channel.id} ({cid}): {name}  (niche {col.niche}; library {col.use_library}; archives {col.archives})",
              flush=True)
        t0 = time.time()
        # find() closes over the module's add_to_library through its own globals: patched above
        sid = col.find(name, show, need=8.0, hook=False)
        rec = dict(col.log[-1]) if col.log else {}
        rec.update(subject=name, channel=cid, result=sid, seconds=round(time.time() - t0),
                   candidates=seen.get("candidates", []), verdicts=seen.get("verdicts", []),
                   picked=seen.get("picked"))
        report.append(rec)
        for c in rec["candidates"]:
            print(f"  cand {c['source']:<16} {c.get('kind') or '':<6} score={c.get('thumb_score')} "
                  f"{(c.get('title') or c.get('error') or '')[:80]}")
        for v in rec["verdicts"]:
            x = v["verdict"] or {}
            print(f"  judge {v['label']}: shows={x.get('shows')} conf={x.get('confidence')} era_ok={x.get('era_ok')} "
                  f"same_shot={x.get('same_shot')} interest={x.get('interest')} now={v.get('passes_now')} "
                  f"no_era={v.get('passes_without_era')} | {x.get('why')}")
        print(f"  -> {sid} picked={rec['picked']} library_checked={rec.get('library')} "
              f"archive_fetched={rec.get('archive')} error={rec.get('error')}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "probe.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
