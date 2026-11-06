"""Footage judge test (research/footage-filter-models.md, 6 Nov 2026): every judge over the 204
round-2 clips Roeland marked good/bad, plus a blind side-by-side of the clip today's check would
pick vs the clip the new judge would pick, per subject, for /bench/footage.

Read-only: it reads ~/meta-out/pieces (round 2 clips, landscape pieces), ~/meta-out/marks/footage.json
(Roeland's marks) and writes ~/meta-out/judgetest/{made.json,report.md}. Costs: one Claude call and
one Gemini call per model per subject (30 subjects).

    python -m meta.collector.judgetest [--limit N] [--gemini gemini-3.8-flash,gemini-3-flash]
"""
from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime
from pathlib import Path

OUT = Path.home() / "meta-out"
HERE = OUT / "judgetest"
PIECES = OUT / "pieces"
SECONDS = 8.0
RUN = datetime.now().strftime("%Y%m%d-%H%M")  # this run's id: page marks are per run (letters differ)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")


def load_subjects(pieces_dir: Path = PIECES, marks_file: Path = OUT / "marks" / "footage.json") -> list[dict]:
    """Round-2 subjects Roeland marked, each with its clips (letter, landscape file, preview, bad?)."""
    marks = json.loads(marks_file.read_text(encoding="utf-8")).get("clips", {}) if marks_file.exists() else {}
    rows = []
    for f in sorted(pieces_dir.glob("made-part*.json")):
        for s in json.loads(f.read_text(encoding="utf-8")).get("subjects", []):
            key = f"{s['niche']}/{s['name']}"
            if key not in marks:
                continue
            bad = set(marks[key])
            clips = []
            for c in s.get("candidates", []):
                land = pieces_dir / "landscape" / f"{s['niche']}-{slug(s['name'])}-{c.get('label')}.mkv"
                if c.get("preview") and land.exists() and "gates" in c:
                    clips.append({"label": c["label"], "path": str(land), "preview": c["preview"],
                                  "bad": c["label"] in bad, "gates": c["gates"], "source": c.get("source", ""),
                                  "old_yes": bool(c.get("claude_shows"))})
            if clips:
                rows.append({"key": key, "niche": s["niche"], "name": s["name"], "clips": clips})
    return rows


def old_pick(s: dict) -> str | None:
    """What today's collector would have used: its order, Claude's 3-frame yes, the gates."""
    from shorts.collector import choose
    made = [dict(c) for c in s["clips"]]
    pick = choose(made, {c["label"] for c in made if c["old_yes"]}, s["niche"])
    return pick["label"] if pick else None


def new_pick(s: dict, verdicts: dict[str, dict]) -> str | None:
    from shorts import judge
    from shorts.collector import choose
    made = [dict(c, verdict=verdicts.get(c["label"])) for c in s["clips"]]
    ok = {lab for lab, v in verdicts.items() if judge.passes(v) and v["interest"] >= judge.MIN_INTEREST}
    pick = choose(made, ok, s["niche"])
    return pick["label"] if pick else None


def metrics(rows: list[dict], name: str) -> dict:
    """How one judge's yes/no matches Roeland's good/bad, and how good its picks are."""
    from shorts import judge
    tp = fp = fn = tn = 0
    good_i, bad_i, picks_bad, picks, none = [], [], 0, 0, 0
    for s in rows:
        v = s["verdicts"].get(name)
        if v is None:
            continue
        for c in s["clips"]:
            if name == "today":
                yes = c["old_yes"]
            else:
                cv = v.get(c["label"])
                yes = judge.passes(cv)
                if cv:
                    (bad_i if c["bad"] else good_i).append(cv["interest"])
            good = not c["bad"]
            tp += yes and good
            fp += yes and not good
            fn += (not yes) and good
            tn += (not yes) and not good
        pick = s["picks"].get(name)
        if pick is None:
            none += 1
        else:
            picks += 1
            picks_bad += next(c["bad"] for c in s["clips"] if c["label"] == pick)
    n = tp + fp + fn + tn
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    return {"clips": n, "agree": round((tp + tn) / n * 100) if n else None,
            "yes_good": round(tp / (tp + fp) * 100) if tp + fp else None,
            "good_kept": round(tp / (tp + fn) * 100) if tp + fn else None,
            "bad_caught": round(tn / (tn + fp) * 100) if tn + fp else None,
            "subjects": sum(1 for s in rows if name in s["verdicts"]),
            "picks": picks, "picks_bad": picks_bad, "no_pick": none,
            "interest_good": avg(good_i), "interest_bad": avg(bad_i)}


def report(res: dict) -> str:
    lines = ["# Footage judge test", "", f"{res['at']}, {res['subjects']} subjects, {res['clips']} clips "
             "Roeland marked in round 2. 'Yes right' = share of the clips the judge passed that Roeland "
             "marked good. 'Picks bad' = subjects where the clip the judge would USE was marked bad.", "",
             "| Judge | Agrees | Yes right | Good kept | Bad caught | Picks bad | No pick | Interest good / bad |",
             "|---|---|---|---|---|---|---|---|"]
    for name, m in res["metrics"].items():
        lines.append(f"| {name} | {m['agree']}% | {m['yes_good']}% | {m['good_kept']}% | {m['bad_caught']}% | "
                     f"{m['picks_bad']}/{m['picks']} | {m['no_pick']} | {m['interest_good']} / {m['interest_bad']} |")
    lines += ["", f"Side by side: {len(res['pairs'])} subjects where the new judge ({res['new']}) picks another "
              f"clip than today; {res['same']} the same clip; {res['only_old']} only today found one; "
              f"{res['only_new']} only the new judge did.", ""]
    for e in res.get("errors", []):
        lines.append(f"- error: {e}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the first N subjects (a quick try)")
    ap.add_argument("--gemini", default="gemini-3.8-flash,gemini-3-flash")
    ap.add_argument("--claude-model", default="", help="Claude Code --model for the claude+ judge")
    a = ap.parse_args(argv)
    from meta.common import open_library
    from shorts import judge, llm
    from shorts.sources import api_key
    settings, _conn, _ = open_library()
    key = api_key(settings, "gemini")
    rows = load_subjects()
    if a.limit:
        rows = rows[:a.limit]
    HERE.mkdir(parents=True, exist_ok=True)
    models = [m.strip() for m in a.gemini.split(",") if m.strip()] if key else []
    errors = [] if key else ["no Gemini key saved: only Claude judges"]
    log(f"{len(rows)} subjects, {sum(len(s['clips']) for s in rows)} clips; Gemini models {models or 'none'}")
    for i, s in enumerate(rows, 1):
        clips = [{"label": c["label"], "path": c["path"], "start": 0.0,
                  "seconds": min(SECONDS, judge_duration(c["path"]))} for c in s["clips"]]
        s["verdicts"] = {"today": {}}
        t0 = time.time()
        for m in models:
            try:
                s["verdicts"][f"gemini {m}"] = judge.gemini_judge(key, clips, s["name"], s["niche"], model=m)
            except Exception as e:  # noqa: BLE001 - the other judges still run
                errors.append(f"{s['key']} gemini {m}: {str(e)[:200]}")
                log(f"  gemini {m} failed: {str(e)[:200]}")
        try:
            s["verdicts"]["claude+"] = judge.claude_judge(settings, clips, s["name"], s["niche"], model=a.claude_model)
        except llm.LLMLimitError as e:
            errors.append(f"Claude usage limit: {e}")
            log("Claude usage limit reached: stopping")
            rows = rows[:i - 1] + [s]
            break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s['key']} claude+: {str(e)[:200]}")
        for m in models:
            g, c = s["verdicts"].get(f"gemini {m}"), s["verdicts"].get("claude+")
            if g and c:
                s["verdicts"][f"both ({m})"] = {lab: v for lab in g if (v := judge.combine(g.get(lab), c.get(lab)))}
        s["picks"] = {"today": old_pick(s)} | {n: new_pick(s, v) for n, v in s["verdicts"].items() if n != "today"}
        log(f"{i}/{len(rows)} {s['key']}: {len(clips)} clips, {time.time() - t0:.0f} s, picks {s['picks']}")
        save(rows, models, errors)
    save(rows, models, errors, final=True)


def judge_duration(path: str) -> float:
    from meta.collector.pieces import duration
    return duration(path) or SECONDS


def best_new(names: list[str], metrics_: dict) -> str:
    """The new judge for the side by side: the 'both' with the fewest bad picks, else claude+."""
    cands = [n for n in names if n.startswith("both")] or [n for n in names if n != "today"]
    return min(cands, key=lambda n: (metrics_[n]["picks_bad"] / max(1, metrics_[n]["picks"]),
                                     -metrics_[n]["agree"] if metrics_[n]["agree"] is not None else 0)) if cands else ""


def save(rows: list[dict], models: list[str], errors: list[str], final: bool = False) -> None:
    done = [s for s in rows if "picks" in s]
    names = sorted({n for s in done for n in s["verdicts"]}, key=lambda n: (n != "today", n))
    mets = {n: metrics(done, n) for n in names}
    new = best_new(names, mets)
    rng = random.Random("judgetest-" + datetime.now().strftime("%Y%m%d"))
    pairs, same, only_old, only_new = [], 0, 0, 0
    for s in done:
        o, n = s["picks"].get("today"), s["picks"].get(new)
        if o and n and o != n:
            letters = ["A", "B"]
            rng.shuffle(letters)
            prev = {c["label"]: c for c in s["clips"]}
            v = s["verdicts"][new].get(n) or {}
            pairs.append({"key": f"{RUN}:{s['key']}", "niche": s["niche"], "name": s["name"],
                          "files": sorted([(letters[0], prev[o]["preview"]), (letters[1], prev[n]["preview"])]),
                          "versions": {letters[0]: "today", letters[1]: "new"},
                          "clips": {letters[0]: o, letters[1]: n}, "new_why": v.get("why", ""),
                          "new_interest": v.get("interest")})
        elif o and n:
            same += 1
        elif o:
            only_old += 1
        elif n:
            only_new += 1
    res = {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "final": final, "models": models,
           "subjects": len(done), "clips": sum(len(s["clips"]) for s in done), "metrics": mets, "new": new,
           "pairs": pairs, "same": same, "only_old": only_old, "only_new": only_new, "errors": errors,
           "rows": [{k: s[k] for k in ("key", "niche", "name", "picks", "verdicts")} |
                    {"clips": [{k: c[k] for k in ("label", "bad", "old_yes", "source", "preview")} for c in s["clips"]]}
                    for s in done]}
    (HERE / "made.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    (HERE / "report.md").write_text(report(res), encoding="utf-8")


if __name__ == "__main__":
    main()
