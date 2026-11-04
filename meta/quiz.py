"""Quiz Shorts (meta maker, phase 1): "Can you name this?" in 3 levels. Each level: the clip with a
3-2-1 countdown, then the answer and one short fact. About 20 seconds. Research: quizzes had the
fastest views per day and the most comments (research/youtube-meta.md).

Footage first: Claude only gets clips it can see, and must name the subject with certainty from
the frame and the archive title. Samples only, never uploads.

    python -m meta.quiz --all -n 1
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from PIL import ImageDraw

from .common import H, W, BAND_Y, blank, clip_strip, verify, candidate_lines, candidates, log, open_library, pick_track, render_segments
from .textcard import font, wrap

OUT = Path.home() / "meta-out" / "quiz"
ASK, COUNT, REVEAL = 1.0, 3, 2.8  # seconds: question alone, countdown steps, answer

# niche: (what is guessed, searches, question)
THEMES = {
    "space": ("planet, moon or spacecraft", ["Saturn planet rings", "Jupiter planet", "Moon surface craters",
              "Space Shuttle launch", "Saturn V rocket launch", "Mars surface rover", "International Space Station",
              "Earth from space"], "Can you name this?"),
    "warplane": ("military aircraft", ["B-17 bomber flying", "P-51 Mustang fighter", "Spitfire plane",
                 "jet fighter takeoff", "B-29 bomber", "military helicopter", "SR-71 Blackbird",
                 "aircraft carrier jets"], "Can you name this plane?"),
    "ocean": ("sea animal", ["shark swimming", "whale", "octopus", "jellyfish", "sea turtle", "dolphins",
              "manta ray", "seal on ice"], "Can you name this sea animal?"),
    "planet": ("wild animal", ["elephant", "lion", "bear", "eagle flying", "giraffe", "zebra herd", "monkey",
               "crocodile"], "Can you name this animal?"),
    "retrotech": ("old machine or device", ["old computer room", "vintage radio", "telephone switchboard",
                  "steam locomotive", "typewriter", "old television set", "early automobile", "printing press"],
                  "What is this machine?"),
    "history": ("famous landmark or historic machine", ["Statue of Liberty", "Eiffel Tower", "Hindenburg airship",
                "pyramids Egypt", "Golden Gate Bridge", "Empire State Building", "zeppelin", "White House"],
                "Can you name this?"),
}

PICK = """You make a "{question}" quiz Short in 3 levels about {theme}. Below are candidate archive
clips; each has an archive title and one frame (the images, in the same order).

Choose up to 5 candidates where you are CERTAIN what the subject is and where the FRAME itself
clearly shows it (the archive title is only a hint: a film titled "Paris" may show Notre-Dame,
not the Eiffel Tower). Never guess: a wrong answer ruins the Short. Order them from easy to
hard. All must be different subjects. If fewer qualify, return fewer.

For each: "answer" (the name, max 4 words) and "fact" (one surprising TRUE fact about it, max 14
plain words).

Candidates:
{lines}

Return JSON only: {{"title": "YouTube title, max 60 characters; the quiz always has exactly 3 levels, e.g. 'Only 1% can name all 3 planes'",
"rounds": [{{"clip": "C3", "answer": "...", "fact": "..."}}]}}"""


def overlay(question: str, level: int, big: str | None, answer: str | None, fact: str | None):
    im = blank()
    d = ImageDraw.Draw(im)
    f_q, f_lvl = font("bold", 66), font("bold", 40)
    d.rounded_rectangle((W // 2 - 140, 300, W // 2 + 140, 362), 30, fill=(255, 215, 0, 255))
    d.text((W // 2, 331), f"LEVEL {level}/3", font=f_lvl, fill="black", anchor="mm")
    y = 410
    for ln in wrap(question, f_q, W - 120):
        d.text((W // 2, y), ln, font=f_q, fill="white", anchor="ma", stroke_width=3, stroke_fill="black")
        y += 80
    below = BAND_Y + 760 + 40
    if big:
        d.text((W // 2, below + 90), big, font=font("bold", 170), fill=(255, 215, 0), anchor="mm",
               stroke_width=6, stroke_fill="black")
    if answer:
        d.text((W // 2, below + 40), answer, font=font("bold", 72), fill=(80, 230, 120), anchor="mm",
               stroke_width=4, stroke_fill="black")
        y = below + 110
        for ln in wrap(fact or "", font("regular", 44), W - 140):
            d.text((W // 2, y), ln, font=font("regular", 44), fill="white", anchor="ma")
            y += 58
    return im


def make(niche: str, n: int, lib: dict | None = None) -> list[dict]:
    """n Shorts for a niche. lib (from shorts/metatype.py, the main program): settings, conn, llm,
    out_dir and the scene ids the channel already used; then ~/meta-out is not touched."""
    settings, conn, llm = (lib["settings"], lib["conn"], lib["llm"]) if lib else open_library()
    theme, queries, question = THEMES[niche]
    out_dir = lib["out_dir"] if lib else OUT / niche
    out_dir.mkdir(parents=True, exist_ok=True)
    done = OUT / "made.json"
    made = [] if lib else json.loads(done.read_text(encoding="utf-8")) if done.exists() else []
    # Samples may reuse clips from earlier runs; a channel doesn't reuse its own. Within one run each clip once.
    used: set[int] = set((lib or {}).get("used", ()))
    results = []
    for k in range(n):
        need = ASK + COUNT + REVEAL + 0.5
        cands = candidates(conn, settings, queries, used, need, per_query=4)
        if len(cands) < 3:
            log(f"[{niche}] not enough footage for a quiz")
            break
        lines, images = candidate_lines(cands)
        log(f"[{niche}] quiz {k + 1}: {len(cands)} candidate clips, asking Claude which it can name for sure")
        data = llm.ask_json(PICK.format(question=question, theme=theme, lines=lines), settings.claude_cmd,
                            retries=1, images=images)
        rounds = [r for r in data.get("rounds", []) if r.get("clip") in cands]
        ok = verify(llm, settings, [(r["clip"], clip_strip(cands[r["clip"]], need), r["answer"])
                                    for r in rounds])
        for r in rounds:
            if r["clip"] not in ok:
                log(f"[{niche}] second look rejected '{r['answer']}'")
        rounds = [r for r in rounds if r["clip"] in ok][:3]
        if len(rounds) < 3:
            log(f"[{niche}] Claude could name only {len(rounds)} clips for sure: skipped")
            break
        segs = []
        for lvl, r in enumerate(rounds, 1):
            clip = cands[r["clip"]]
            used.add(clip["id"])
            t = 0.0
            segs.append({"clip": clip, "offset": t, "seconds": ASK, "overlay": overlay(question, lvl, None, None, None)})
            t += ASK
            for c in range(COUNT, 0, -1):
                segs.append({"clip": clip, "offset": t, "seconds": 1.0, "overlay": overlay(question, lvl, str(c), None, None)})
                t += 1.0
            segs.append({"clip": clip, "offset": t, "seconds": REVEAL,
                         "overlay": overlay(question, lvl, None, r["answer"], r.get("fact"))})
        idx = sum(1 for m in made if m["niche"] == niche) + len(results) + 1
        out = out_dir / (f"quiz-{datetime.now():%Y%m%d-%H%M%S}-{idx}.mp4" if lib else f"{niche}-quiz-{idx:03d}.mp4")
        track = pick_track(conn)
        secs = render_segments(segs, track, out)
        rec = {"niche": niche, "file": str(out), "title": data.get("title", ""), "seconds": round(secs, 1),
               "rounds": [{"answer": r["answer"], "fact": r.get("fact"), "footage": cands[r["clip"]]["title"]}
                          for r in rounds],
               "scene_ids": [cands[r["clip"]]["id"] for r in rounds],
               "music": track["path"] if track else None,
               "made_at": datetime.now().isoformat(timespec="seconds")}
        results.append(rec)
        log(f"[{niche}] {out.name}: {rec['title']} ({secs:.0f} s: {', '.join(r['answer'] for r in rounds)})")
    if not lib:
        done.write_text(json.dumps(made + results, indent=1, ensure_ascii=False), encoding="utf-8")
    return results


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--niche", choices=sorted(THEMES))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-n", type=int, default=1)
    a = ap.parse_args(argv)
    niches = sorted(THEMES) if a.all else [a.niche or "warplane"]
    total = []
    for i, niche in enumerate(niches, 1):
        log(f"quiz niche {i}/{len(niches)}: {niche}")
        try:
            total += make(niche, a.n)
        except Exception as e:  # noqa: BLE001
            log(f"[{niche}] failed: {e}")
    log(f"quiz done: {len(total)} Shorts in {OUT}")


if __name__ == "__main__":
    main()
