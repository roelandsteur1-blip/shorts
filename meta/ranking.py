"""Ranking Shorts (meta maker, phase 1): "Ranking X by Y", 5 items. The list 1-5 sits above the
video; items are revealed in shuffled order, each with its clip, until #1. About 18 seconds.
Research: rankings had the highest median views per Short (research/youtube-meta.md).

Footage first: Claude only ranks subjects it can see and name for sure, by a number it is sure of.
Samples only, never uploads.

    python -m meta.ranking --all -n 1
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime
from pathlib import Path

from PIL import ImageDraw

from .common import W, blank, clip_strip, verify, candidate_lines, candidates, log, open_library, pick_track, render_segments
from .quiz import THEMES
from .textcard import font

OUT = Path.home() / "meta-out" / "ranking"
INTRO, ITEM, OUTRO = 1.5, 3.0, 2.0

# niche: what the ranking may be about (Claude picks one measurable property that fits the clips)
PROPERTIES = {
    "space": "size, distance from the Sun, or launch mass",
    "warplane": "top speed, first flight year, or weight",
    "ocean": "length or weight",
    "planet": "top speed, weight or lifespan",
    "retrotech": "year it was invented (oldest = #1)",
    "history": "height or year it was built",
}

PICK = """You make a "ranking" Short about {theme}. Below are candidate archive clips; each has an
archive title and one frame (the images, in the same order).

Choose up to 8 candidates where you are CERTAIN what the subject is and where the FRAME itself
clearly shows it (the archive title is only a hint), all different subjects that can be ranked by ONE measurable property:
{props}. Every value must measure the SAME thing in the SAME unit (never mix width with height
or length), and no two items may share a value. Use only numbers you are sure of
(well-documented figures). If fewer than 4 qualify, return fewer.

Rank them: #1 = the most/highest (or as stated above). Each item: "name" (max 4 words) and
"value" (the number with unit, e.g. "703 km/h").

Candidates:
{lines}

Return JSON only: {{"title": "YouTube title, max 60 characters, e.g. 'Ranking WWII planes by speed'",
"heading": "on-screen heading, max 5 words, true for EVERY item, e.g. 'Ranked by top speed' (not 'Fastest animals' when a penguin is in the list)",
"items": [{{"rank": 1, "clip": "C2", "name": "...", "value": "..."}}]}}"""


def overlay(heading: str, items: list[dict], shown: set[int], current: int | None):
    im = blank()
    d = ImageDraw.Draw(im)
    d.text((W // 2, 300), heading, font=font("bold", 66), fill="white", anchor="ma",
           stroke_width=4, stroke_fill="black")
    f_row, f_val = font("bold", 46), font("regular", 38)
    y = 400
    for it in sorted(items, key=lambda x: x["rank"]):
        r = it["rank"]
        hot = r == current
        colour = (255, 215, 0) if hot else (255, 255, 255)
        d.text((70, y), f"{r}.", font=f_row, fill=colour)
        if r in shown:
            d.text((140, y), it["name"], font=f_row, fill=colour)
            d.text((W - 70, y + 6), it["value"], font=f_val, fill=(200, 200, 200) if not hot else colour,
                   anchor="ra")
        y += 58
    return im


def make(niche: str, n: int) -> list[dict]:
    settings, conn, llm = open_library()
    theme, queries, _ = THEMES[niche]
    out_dir = OUT / niche
    out_dir.mkdir(parents=True, exist_ok=True)
    done = OUT / "made.json"
    made = json.loads(done.read_text(encoding="utf-8")) if done.exists() else []
    used = {sid for m in made for sid in m["scene_ids"]}
    results = []
    for k in range(n):
        cands = candidates(conn, settings, queries, used, INTRO + ITEM + OUTRO + 0.5, per_query=2)
        if len(cands) < 4:
            log(f"[{niche}] not enough footage for a ranking")
            break
        lines, images = candidate_lines(cands)
        log(f"[{niche}] ranking {k + 1}: {len(cands)} candidate clips, asking Claude to pick and rank 5")
        data = llm.ask_json(PICK.format(theme=theme, props=PROPERTIES[niche], lines=lines),
                            settings.claude_cmd, retries=1, images=images)
        items = [it for it in data.get("items", []) if it.get("clip") in cands]
        ok = verify(llm, settings, [(it["clip"], clip_strip(cands[it["clip"]], INTRO + ITEM + OUTRO), it["name"])
                                    for it in items])
        for it in items:
            if it["clip"] not in ok:
                log(f"[{niche}] second look rejected '{it['name']}'")
        items = sorted((it for it in items if it["clip"] in ok), key=lambda x: int(x.get("rank", 99)))[:5]
        if len(items) < 4:  # 4 is enough for a ranking; most runs only found 3-4 sure ones
            log(f"[{niche}] Claude could rank only {len(items)} clips for sure: skipped")
            break
        for i, it in enumerate(sorted(items, key=lambda x: int(x.get("rank", 99))), 1):
            it["rank"] = i
        order = [it for it in items if it["rank"] != 1]
        random.shuffle(order)  # shuffled, not a countdown; #1 always last
        order.append(next(it for it in items if it["rank"] == 1))
        heading = data.get("heading") or data.get("title", "")
        shown: set[int] = set()
        segs = [{"clip": cands[order[0]["clip"]], "offset": 0, "seconds": INTRO,
                 "overlay": overlay(heading, items, shown, None)}]
        for it in order:
            shown.add(it["rank"])
            off = INTRO if it is order[0] else 0
            segs.append({"clip": cands[it["clip"]], "offset": off, "seconds": ITEM,
                         "overlay": overlay(heading, items, set(shown), it["rank"])})
        segs[-1]["seconds"] += OUTRO
        for it in items:
            used.add(cands[it["clip"]]["id"])
        idx = sum(1 for m in made if m["niche"] == niche) + len(results) + 1
        out = out_dir / f"{niche}-ranking-{idx:03d}.mp4"
        secs = render_segments(segs, pick_track(conn), out)
        rec = {"niche": niche, "file": str(out), "title": data.get("title", ""), "heading": heading,
               "seconds": round(secs, 1),
               "items": [{"rank": it["rank"], "name": it["name"], "value": it["value"],
                          "footage": cands[it["clip"]]["title"]} for it in sorted(items, key=lambda x: x["rank"])],
               "scene_ids": [cands[it["clip"]]["id"] for it in items],
               "made_at": datetime.now().isoformat(timespec="seconds")}
        results.append(rec)
        log(f"[{niche}] {out.name}: {rec['title']} ({secs:.0f} s)")
    done.write_text(json.dumps(made + results, indent=1, ensure_ascii=False), encoding="utf-8")
    return results


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--niche", choices=sorted(PROPERTIES))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-n", type=int, default=1)
    a = ap.parse_args(argv)
    niches = sorted(PROPERTIES) if a.all else [a.niche or "warplane"]
    total = []
    for i, niche in enumerate(niches, 1):
        log(f"ranking niche {i}/{len(niches)}: {niche}")
        try:
            total += make(niche, a.n)
        except Exception as e:  # noqa: BLE001
            log(f"[{niche}] failed: {e}")
    log(f"ranking done: {len(total)} Shorts in {OUT}")


if __name__ == "__main__":
    main()
