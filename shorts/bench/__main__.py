"""python -m shorts.bench [options] <stage>  (stages: see shorts/bench/__init__.py; 'all' runs them in order)"""
from __future__ import annotations

import argparse
import logging
import sys

from ..config import load_settings
from . import load, root

STAGES = ["testset", "frames", "embed", "asr", "rank", "judge", "clips", "clip-judge", "interest", "chains",
          "report"]
# 'all': narration is only fetched for the videos some method ranks in its top 20 (re-reading
# every video's audio from the archives would take hours), then everything is ranked again.
ALL = ["frames", "embed", "rank", "asr", "rank", "judge", "clips", "clip-judge", "interest", "chains", "report"]
POOL_SIZE = 30


def pool_methods(ranks: dict) -> list[str]:
    ms = ["today"]
    for m in ranks:
        if m.endswith("/max3+meta") or m.endswith("/mid+meta") or m.endswith("/mid+meta+asr"):
            ms.append(m)
    return ms


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m shorts.bench", description=__doc__)
    p.add_argument("stage", choices=STAGES + ["all", "status"])
    p.add_argument("--config-dir")
    p.add_argument("--data-dir")
    p.add_argument("--blocks", type=int, default=60)
    p.add_argument("--library", type=int, default=4000, help="scenes in the bench library")
    p.add_argument("--clips", type=int, default=40, help="clips for the seconds and crop tests")
    p.add_argument("--moments", type=int, default=20, help="moments rendered three ways for the blind review")
    p.add_argument("--threads", type=int, default=4, help="CPU threads for the models (leave some for the daily run)")
    p.add_argument("--models", help="comma separated, default: all (see shorts/bench/models.py)")
    p.add_argument("--finders", default="yoloe,yoloworld,omdet,u2netp")
    p.add_argument("--no-asr", action="store_true", help="skip the narration test (it re-reads audio from the archives)")
    p.add_argument("--asr-videos", type=int, help="transcribe at most this many videos")
    p.add_argument("--judge-limit", type=int, help="at most this many Claude calls per judge stage in this run")
    p.add_argument("--judge-every", type=float, default=300,
                   help="seconds between Claude calls (default 5 min: leaves the Pro limit room for the daily run)")
    p.add_argument("--smoke", action="store_true", help="tiny trial run in data_dir/bench-smoke")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    from .. import bench
    if a.smoke:
        bench.FOLDER = "bench-smoke"
        a.blocks, a.library, a.clips, a.moments = 4, 300, 3, 2
        a.models = a.models or "clip-b32,siglip2-b16"
        a.finders = "u2netp"
        a.judge_limit = 2 if a.judge_limit is None else a.judge_limit
        a.asr_videos = 2
        a.judge_every = 0
    settings = load_settings(a.config_dir, data_dir=a.data_dir)
    from ..server import load_env
    load_env(settings)  # the Claude token on the server, for the judge
    from .. import llm
    from . import asr, chains, clips, frames, interest, judge, models, rank, report, testset

    if a.stage == "status":
        lines = load(root(settings) / "progress.json", {}) or {"-": {"stage": "not started", "done": 0, "total": 0}}
        for st in lines.values():
            print(f"{st.get('stage')}: {st.get('done', 0)}/{st.get('total', 0)} {st.get('note', '')}")
        if (root(settings) / "report.md").exists():
            print(f"report: {root(settings) / 'report.md'}")
        return 0
    names = a.models.split(",") if a.models else list(models.MODELS)
    stages = ALL if a.stage == "all" else [a.stage]
    ts = testset.build(settings, a.blocks, a.library)
    todo = [n for n in names if not models.emb_path(settings, n).exists()]
    for stage in stages:
        logging.info("stage %s", stage)
        if stage == "frames":
            frames.extract(settings, ts, workers=max(1, a.threads))
        elif stage == "embed":
            models.embed_all(a.config_dir, a.data_dir, bench.FOLDER, todo, a.threads)
        elif stage == "asr" and not a.no_asr:
            ranks = load(root(settings) / "rank.json", {})
            found = {ts["scenes"][str(sid)]["asset_id"] for m, per in ranks.items() for lst in per.values()
                     for sid, _ in lst if str(sid) in ts["scenes"]}
            asr.run(settings, ts, threads=a.threads, max_assets=a.asr_videos, only=found or None)
        elif stage == "rank":
            rank.run(settings, ts)
        elif stage == "judge":
            ranks = load(root(settings) / "rank.json", {})
            judge.run_topic(settings, ts, rank.pool(ts, ranks, pool_methods(ranks), POOL_SIZE), a.judge_limit,
                            a.judge_every)
        elif stage == "clips":
            labels = load(root(settings) / "judge.json", {})
            clips.measure(settings, ts, labels, a.clips, a.threads, a.finders.split(",") if a.finders else [])
        elif stage == "clip-judge":
            clips.run_judge(settings, ts, a.judge_limit, a.judge_every)
        elif stage == "interest":
            interest.run(settings, ts, load(root(settings) / "judge.json", {}), a.threads)
        elif stage == "chains":
            names_ok = [n for n in names if models.emb_path(settings, n).exists()]
            if not names_ok:
                logging.warning("no embeddings yet: run the embed stage first")
                continue
            search = chains.SEARCH if chains.MATCH in names_ok else f"{names_ok[-1]}/max3+meta"
            match = chains.MATCH if chains.MATCH in names_ok else names_ok[-1]
            chains.run(settings, ts, a.moments, a.threads, search, match,
                       finder=chains.FINDER if chains.FINDER in a.finders.split(",") else None)
        elif stage == "report":
            print(report.build(settings))
        if stage in ("judge", "clip-judge") and llm.limit_reached():
            # Later stages on half the labels would only have to be redone: stop, continue later.
            logging.warning("Claude usage limit reached: stopping here. Start again after it resets; "
                            "everything done so far is kept.")
            report.build(settings)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
