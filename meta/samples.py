"""Render samples of every meta format in one go and pack only the new ones for review.

    python -m meta.samples            # text cards (2 per niche), 1 quiz and 1 ranking per niche
"""
from __future__ import annotations

import argparse
import json
import tarfile
from pathlib import Path

from . import quiz, ranking, textcard
from .textcard import log

PACK = Path.home() / "meta-out" / "samples.tar.gz"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cards", type=int, default=2, help="text cards per niche")
    ap.add_argument("--quizzes", type=int, default=1, help="quizzes per niche")
    ap.add_argument("--rankings", type=int, default=1, help="rankings per niche")
    a = ap.parse_args(argv)
    made = []
    jobs = [("text card", textcard.make, sorted(textcard.NICHES), a.cards, True),
            ("quiz", quiz.make, sorted(quiz.THEMES), a.quizzes, False),
            ("ranking", ranking.make, sorted(ranking.PROPERTIES), a.rankings, False)]
    for name, fn, niches, n, avoid_arg in jobs:
        if n <= 0:
            continue
        for i, niche in enumerate(niches, 1):
            log(f"== {name}s, niche {i}/{len(niches)}: {niche}")
            try:
                made += fn(niche, n, []) if avoid_arg else fn(niche, n)
            except Exception as e:  # noqa: BLE001 - one niche failing must not stop the rest
                log(f"[{niche}] {name} failed: {e}")
    index = PACK.with_suffix("").with_suffix(".json")
    index.write_text(json.dumps(made, indent=1, ensure_ascii=False), encoding="utf-8")
    with tarfile.open(PACK, "w:gz") as tf:
        tf.add(index, arcname="samples/index.json")
        for m in made:
            p = Path(m["file"])
            if p.exists():
                tf.add(p, arcname=f"samples/{p.parent.parent.name}/{p.name}")
    log(f"done: {len(made)} new Shorts packed in {PACK}")


if __name__ == "__main__":
    main()
