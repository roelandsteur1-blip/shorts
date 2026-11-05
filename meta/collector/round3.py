"""Footage collector research R9 (round 3): real Shorts, new collector versus today's.

For each quiz/ranking niche: one ranking and one quiz made the way the dashboard makes them today
(footage first, from the library), and one of each made topic first with the new collector:

1. Claude picks the subjects (5 to rank, 3 to name) without seeing footage.
2. Per subject the collector searches the sources Roeland judged good in round 1, fetches the
   piece of the best few hits, drops black and text-heavy clips (the only gates that mattered in
   round 2), and keeps the first clip Claude's blind check says shows the subject (92% good in
   round 2). Videos before photos; for history the old-looking clip first (old_look > AGE_CUT).
3. The Short is rendered with the same overlays and band layout as today's.

The two versions get random letters (key in made-partN.json), so Roeland judges blind.
Read-only on the library: nothing is added to it, nothing is uploaded.

    python -m meta.collector.round3 --part 1
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from meta.collector import pieces, sweep

OUT = Path.home() / "meta-out" / "round3"
NICHES = ["space", "warplane", "ocean", "planet", "history", "retrotech"]  # the quiz/ranking niches
AGE_CUT = 0.02          # old_look above this = old film (round 2, Roeland's oldcity check)
OLD_NICHES = {"history", "retrotech"}
VIDEO_FIRST = ("library", "commons_video", "nasa", "pixabay")
TRY_FIRST = 3           # pieces fetched before the first check; the rest only if none passes

RANK_PROMPT = """You make a "ranking" Short about {theme}. Pick 5 different, well-known subjects that
can be ranked by ONE measurable property: {props}. Every value must measure the SAME thing in the
SAME unit, no two items share a value, and use only well-documented numbers you are sure of.
Pick subjects that are easy to film or photograph and that archive footage likely exists for
(famous named things, not abstract ones). #1 = the most/highest (or as stated above).

Return JSON only: {{"title": "YouTube title, max 60 characters",
"heading": "on-screen heading, max 5 words, true for EVERY item",
"items": [{{"rank": 1, "name": "max 4 words", "value": "number with unit",
"show": "what a good shot of it looks like, max 10 words"}}]}}"""

QUIZ_PROMPT = """You make a "{question}" quiz Short in 3 levels about {theme}. Pick 3 different,
well-known subjects, easy to hard, that a viewer can recognise from a picture and that archive
footage likely exists for. For each: "answer" (the name, max 4 words), "fact" (one surprising TRUE
fact, max 14 plain words) and "show" (what a good shot of it looks like, max 10 words).

Return JSON only: {{"title": "YouTube title, max 60 characters, e.g. 'Only 1% can name all 3 planes'",
"rounds": [{{"answer": "...", "fact": "...", "show": "..."}}]}}"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def order(cands: list[dict]) -> list[dict]:
    """Videos before photos; within each, the archive's PE-Core thumbnail score."""
    return sorted(cands, key=lambda c: (c["source"] not in VIDEO_FIRST, -(c.get("thumb_score") or 0)))


def choose(made: list[dict], ok: set[str], niche: str) -> dict | None:
    """The clip to use: passed the blind check and the gates; for old niches an old-looking one first."""
    good = [c for c in made if c["label"] in ok and not ({"black", "text on screen"} & set(c["gates"]["faults"]))]
    if niche in OLD_NICHES:
        old = [c for c in good if (c["gates"].get("old_look") or 0) > AGE_CUT]
        good = old + [c for c in good if c not in old]
    return good[0] if good else None


def collect(subject: dict, need: float, lib, scorer, gates, keys: dict, tmp: Path, ask) -> tuple[dict | None, dict]:
    """One clip of `need` seconds for the subject, or None. Returns (clip for render_segments, log)."""
    rec = {"name": subject["name"], "tried": []}
    t0 = time.time()
    # own folder per subject: pieces are named by label (K0, K1...), so subjects sharing a folder
    # overwrote each other's clips and a Short showed one subject's footage for several items
    tmp = tmp / ("".join(ch if ch.isalnum() else "_" for ch in subject["name"].lower()) or "subject")
    tmp.mkdir(parents=True, exist_ok=True)
    cands = [c for c in pieces.candidates(subject, lib[:2] if lib else None, scorer, keys) if "error" not in c]
    cands = order(cands)
    for i, c in enumerate(cands):
        c["label"] = f"K{i}"
    made, checked = [], set()
    clip_was = pieces.CLIP
    try:
        pick = _fetch_until_pass(subject, cands, need, scorer, gates, tmp, ask, rec, made, checked)
    finally:
        pieces.CLIP = clip_was
    rec["seconds"] = round(time.time() - t0, 1)
    if not pick:
        rec["result"] = "nothing passed"
        return None, rec
    rec.update(result=pick["source"], title=pick.get("title"), old_look=pick["gates"].get("old_look"),
               faults=pick["gates"]["faults"])
    path = pick["_path"]
    w, h = probe_size(path)
    clip = {"id": -abs(hash((subject["name"], str(path)))) % 10**9 - 1, "path": str(path), "start": 0.0,
            "end": pieces.duration(str(path)) or need, "width": w, "height": h, "title": pick.get("title", ""),
            "source": pick["source"]}
    return clip, rec


def _fetch_until_pass(subject, cands, need, scorer, gates, tmp, ask, rec, made, checked) -> dict | None:
    pick = None
    for batch in (cands[:TRY_FIRST], cands[TRY_FIRST:]):
        items = []
        for c in batch:
            try:
                pieces.CLIP = max(need, 3.0)
                path = (pieces.fetch_photo(c, tmp, seconds=need) if c.get("kind") == "photo"
                        else pieces.fetch_video(c, subject, scorer, tmp))
                frames = pieces.clip_frames(path, pieces.duration(str(path)) or need)
                c["gates"] = gates.check(frames)
                c["_path"] = path
                made.append(c)
                items.append((c["label"], pieces.strip(frames), subject["name"]))
            except Exception as e:
                c["error"] = str(e)[:200]
            rec["tried"].append({k: c.get(k) for k in ("source", "title", "error")})
        ok = ask(items) if ask and items else {lab for lab, _, _ in items}
        checked |= ok
        pick = choose(made, checked, subject["niche"])
        if pick:
            break
    return pick


def probe_size(path: Path) -> tuple[int, int]:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    w, h = [int(x) for x in p.stdout.strip().split(",")[:2]]
    return w, h


def subject(niche: str, name: str, show: str | None) -> dict:
    return {"niche": niche, "name": name, "wikidata": name, "show": show or f"a clear view of {name}"}


def new_ranking(niche: str, lib, scorer, gates, keys, tmp: Path, ask, out: Path) -> dict:
    from meta import ranking
    from meta.common import pick_track, render_segments
    settings, conn, llm = lib
    theme = ranking.THEMES[niche][0]
    data = llm.ask_json(RANK_PROMPT.format(theme=theme, props=ranking.PROPERTIES[niche]), settings.claude_cmd, retries=1)
    need = ranking.INTRO + ranking.ITEM + ranking.OUTRO + 0.5
    items, logs = [], []
    for it in sorted(data.get("items", []), key=lambda x: int(x.get("rank", 99)))[:5]:
        log(f"  [{niche}] ranking subject: {it['name']}")
        clip, rec = collect(subject(niche, it["name"], it.get("show")), need, lib, scorer, gates, keys, tmp, ask)
        logs.append(rec)
        if clip:
            items.append(dict(it, clip=clip))
    if len(items) < 4:
        return {"error": f"only {len(items)} of 5 subjects found footage", "subjects": logs, "plan": data}
    for i, it in enumerate(items, 1):
        it["rank"] = i
    seq = [it for it in items if it["rank"] != 1]
    random.shuffle(seq)
    seq.append(items[0])
    heading = data.get("heading") or data.get("title", "")
    shown: set[int] = set()
    segs = [{"clip": seq[0]["clip"], "offset": 0, "seconds": ranking.INTRO, "overlay": ranking.overlay(heading, items, shown, None)}]
    for it in seq:
        shown.add(it["rank"])
        off = ranking.INTRO if it is seq[0] else 0
        segs.append({"clip": it["clip"], "offset": off, "seconds": ranking.ITEM,
                     "overlay": ranking.overlay(heading, items, set(shown), it["rank"])})
    segs[-1]["seconds"] += ranking.OUTRO
    secs = render_segments(segs, pick_track(conn), out)
    return {"title": data.get("title"), "heading": heading, "seconds": round(secs, 1), "subjects": logs,
            "items": [{"rank": it["rank"], "name": it["name"], "value": it.get("value"), "source": it["clip"]["source"],
                       "footage": it["clip"]["title"]} for it in items]}


def new_quiz(niche: str, lib, scorer, gates, keys, tmp: Path, ask, out: Path) -> dict:
    from meta import quiz
    from meta.common import pick_track, render_segments
    settings, conn, llm = lib
    theme, _, question = quiz.THEMES[niche]
    data = llm.ask_json(QUIZ_PROMPT.format(question=question, theme=theme), settings.claude_cmd, retries=1)
    need = quiz.ASK + quiz.COUNT + quiz.REVEAL + 0.5
    rounds, logs = [], []
    for r in data.get("rounds", [])[:3]:
        log(f"  [{niche}] quiz subject: {r['answer']}")
        clip, rec = collect(subject(niche, r["answer"], r.get("show")), need, lib, scorer, gates, keys, tmp, ask)
        logs.append(rec)
        if clip:
            rounds.append(dict(r, clip=clip))
    if len(rounds) < 3:
        return {"error": f"only {len(rounds)} of 3 subjects found footage", "subjects": logs, "plan": data}
    segs = []
    for lvl, r in enumerate(rounds, 1):
        t = 0.0
        segs.append({"clip": r["clip"], "offset": t, "seconds": quiz.ASK, "overlay": quiz.overlay(question, lvl, None, None, None)})
        t += quiz.ASK
        for c in range(quiz.COUNT, 0, -1):
            segs.append({"clip": r["clip"], "offset": t, "seconds": 1.0, "overlay": quiz.overlay(question, lvl, str(c), None, None)})
            t += 1.0
        segs.append({"clip": r["clip"], "offset": t, "seconds": quiz.REVEAL,
                     "overlay": quiz.overlay(question, lvl, None, r["answer"], r.get("fact"))})
    secs = render_segments(segs, pick_track(conn), out)
    return {"title": data.get("title"), "seconds": round(secs, 1), "subjects": logs,
            "rounds": [{"answer": r["answer"], "fact": r.get("fact"), "source": r["clip"]["source"],
                        "footage": r["clip"]["title"]} for r in rounds]}


def old_short(kind: str, niche: str, lib, out_dir: Path) -> dict:
    """Today's way: the meta maker itself, footage first from the library."""
    from meta import quiz, ranking
    settings, conn, llm = lib
    mod = quiz if kind == "quiz" else ranking
    res = mod.make(niche, 1, {"settings": settings, "conn": conn, "llm": llm, "out_dir": out_dir, "used": set()})
    if not res:
        return {"error": "today's maker made nothing (see log)"}
    r = res[0]
    return r | {"_file": r["file"]}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", type=int, choices=(1, 2), default=1)
    ap.add_argument("--niche", choices=NICHES)
    ap.add_argument("--new-only", action="store_true", help="remake only the new versions; keep today's from the last run")
    a = ap.parse_args(argv)
    if pieces.RESEARCH_PY.exists() and Path(sys.prefix).resolve() != pieces.RESEARCH_PY.parent.parent.resolve() \
            and not os.environ.get("PIECES_NO_REEXEC"):
        os.execv(str(pieces.RESEARCH_PY), [str(pieces.RESEARCH_PY), "-m", "meta.collector.round3", *(argv or sys.argv[1:])])
    try:
        os.nice(10)
    except OSError:
        pass
    niches = [a.niche] if a.niche else NICHES[:3] if a.part == 1 else NICHES[3:]
    lib = sweep.open_library()
    if not lib:
        raise SystemExit("no library")
    keys = sweep.load_keys(lib[0])
    scorer = sweep.Scorer(lib[0])
    gates = pieces.Gates(scorer)
    from meta.common import verify
    from shorts import llm
    settings = lib[0]
    lib = (lib[0], lib[1], llm)  # sweep gives (settings, conn); the makers here also need llm
    ask = lambda items: verify(llm, settings, items)
    shorts_dir = OUT / "shorts"
    shorts_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"round3-part{a.part}-{datetime.now():%Y%m%d%H%M}")
    made_file = OUT / f"made-part{a.part}.json"
    rows, t_all = [], time.time()
    earlier = {}
    if a.new_only and made_file.exists():
        earlier = {(r["niche"], r["kind"]): r for r in json.loads(made_file.read_text(encoding="utf-8")).get("shorts", [])}
    with tempfile.TemporaryDirectory(prefix="round3-") as tmpd:
        for niche in niches:
            for kind in ("ranking", "quiz"):
                letters = rng.sample("ABCDEFGHJKLMNPQRSTUVWXYZ", 2)
                row = {"niche": niche, "kind": kind, "key": {}}
                prev = earlier.get((niche, kind))
                if prev:  # same letters, today's version kept as it was
                    by_version = {v: l for l, v in prev["key"].items()}
                    letters = [by_version["new"], by_version["today"]]
                    row["key"][letters[1]] = "today"
                    row["today"] = prev.get("today", {})
                    row["remade"] = prev.get("remade", 0) + 1  # the review page asks again for a remade pair
                for version, letter in zip(("new", "today"), letters):
                    if version in row:
                        continue
                    t0 = time.time()
                    dst = shorts_dir / f"{niche}-{kind}-{letter}.mp4"
                    dst.unlink(missing_ok=True)
                    log(f"{niche} {kind} ({version})")
                    try:
                        if version == "new":
                            tmp = Path(tmpd) / f"{niche}-{kind}"
                            tmp.mkdir(exist_ok=True)
                            fn = new_ranking if kind == "ranking" else new_quiz
                            res = fn(niche, lib, scorer, gates, keys, tmp, ask, dst)
                            shutil.rmtree(tmp, ignore_errors=True)
                        else:
                            tmp_out = Path(tmpd) / "old"
                            tmp_out.mkdir(exist_ok=True)
                            res = old_short(kind, niche, lib, tmp_out)
                            if "_file" in res:
                                shutil.move(res.pop("_file"), dst)
                    except Exception as e:  # never lose the run to one Short
                        res = {"error": str(e)[:300]}
                    res["minutes"] = round((time.time() - t0) / 60, 1)
                    if dst.exists():
                        res["file"] = dst.name
                    row["key"][letter] = version
                    row[version] = res
                    log(f"  {version}: {res.get('error') or res.get('title')} ({res['minutes']} min)")
                rows.append(row)
                made_file.write_text(json.dumps({"part": a.part, "shorts": rows,
                                                 "minutes": round((time.time() - t_all) / 60, 1)},
                                                indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    log(f"done: {len(rows)} pairs in {(time.time() - t_all) / 60:.0f} min")


if __name__ == "__main__":
    main()
