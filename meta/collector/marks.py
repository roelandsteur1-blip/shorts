"""Runner job "marks": print a summary of Roeland's marks from the /bench/footage review pages. The
runner copies ~/meta-out/marks/*.json into the results repo."""
from __future__ import annotations

import json
from pathlib import Path

PATH = Path.home() / "meta-out" / "marks" / "footage.json"


def main() -> None:
    try:
        m = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("no marks yet")
        return
    clips, crops = m.get("clips", {}), m.get("crops", {})
    print(f"round 2: {len(clips)} subjects marked, {sum(len(v) for v in clips.values())} clips bad")
    print(f"round 2b: {len(crops)} grids marked")
    print(f"round 3: {len(m.get('shorts', {}))} pairs marked")
    print(f"footage filter: {len(m.get('judge', {}))} pairs marked")


if __name__ == "__main__":
    main()
