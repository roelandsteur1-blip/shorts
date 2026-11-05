"""Source test per channel (footage collector plan, step 1): search every footage archive for 6
sample subjects of the channel's niche and show the best thumbnails per archive on one sheet, so
Roeland can tick which archives the channel uses. Same search and sheet as research round 1
(meta/collector/sweep.py). Search only: nothing is downloaded into the library.

    python -m shorts --channel space source-test --subjects "Saturn|Moon|Space Shuttle"
"""
from __future__ import annotations

import json
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .config import Channel, Settings

LIBRARY = "Library today"
# Roeland's round-1 picks (samples/footage/round1/review.md): the default for a channel that hasn't
# run the test yet. Warplane's choice was the default for the niches he didn't judge.
DEFAULT_PICK = [LIBRARY, "Commons video (name)", "Commons photos (depicts)", "Openverse photos"]
ROUND1_PICKS = {
    "space": DEFAULT_PICK + ["NASA video", "Pixabay (B-roll)"],
    "retrotech": [LIBRARY, "Openverse photos"],
}
SAMPLE = 6


def folder(settings: Settings, cid: str) -> Path:
    return settings.data_dir / "source-test" / cid


def niche(channel: Channel) -> str | None:
    from .metatype import niche_for
    return niche_for(channel)


def default_subjects(channel: Channel) -> list[str]:
    """The research subjects for the channel's niche, else its own search terms."""
    from meta.collector.sweep import SUBJECTS
    n = niche(channel)
    subs = [s["name"] for s in tomllib.loads(SUBJECTS.read_text(encoding="utf-8"))["subject"] if s["niche"] == n]
    return (subs or list(channel.search_terms))[:SAMPLE]


def picked(channel: Channel) -> list[str]:
    """The archives the channel uses: its saved ticks, else Roeland's round-1 pick for its niche."""
    if channel.collector_sources:
        return list(channel.collector_sources)
    return list(ROUND1_PICKS.get(niche(channel) or "", DEFAULT_PICK))


def load(settings: Settings, cid: str) -> dict | None:
    p = folder(settings, cid) / "result.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run(conn, settings: Settings, channel: Channel, names: list[str]) -> dict:
    from meta.collector import sweep
    from . import llm
    names = [n.strip() for n in names if n.strip()][:12] or default_subjects(channel)
    out = folder(settings, channel.id)
    out.mkdir(parents=True, exist_ok=True)
    sweep.ASK = lambda prompt: llm.ask_json(prompt, settings.claude_cmd, retries=1)
    keys = sweep.load_keys(settings)
    scorer = sweep.Scorer(settings)
    srcs = sweep.sources_for(keys)
    columns = [LIBRARY] + [n for n, _, k in srcs if not k or keys.get(k)]
    n = niche(channel) or channel.id
    subjects = [{"niche": n, "name": s, "wikidata": s, "hint": "", "show": f"a clear view of {s}"} for s in names]
    matches = []
    for s in subjects:
        try:
            matches.append(sweep.wikidata_matches(s["wikidata"]))
        except Exception as e:
            print(f"Wikidata lookup failed for {s['name']}: {e}", flush=True)
            matches.append([])
    try:
        qids = sweep.claude_qids(subjects, matches, sweep.ASK)
    except Exception as e:
        print(f"Claude couldn't pick Wikidata items: {e}", flush=True)
        qids = {}
    rows, block, t0 = [], [], time.time()
    for i, s in enumerate(subjects):
        print(f"PROGRESS {i}/{len(subjects)} searching for {s['name']}", flush=True)
        row = {"name": s["name"], "qid": qids.get(i), "sources": {}}
        hits = sweep.sweep_subject(s, qids.get(i), row, (settings, conn), srcs, keys, scorer)
        block.append((s, hits))
        rows.append(row)
    print(f"PROGRESS {len(subjects)}/{len(subjects)} making the sheet", flush=True)
    sweep.niche_sheet(f"{channel.name}", block, columns, out / "sheet.jpg")
    usable = {}
    for col in columns:
        usable[col] = sum(1 for r in rows if _usable(r["sources"].get(col, {})))
    result = {"channel": channel.id, "niche": n, "subjects": names, "columns": columns, "usable": usable,
              "rows": rows, "minutes": round((time.time() - t0) / 60, 1),
              "at": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    (out / "result.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return result


def _usable(src: dict) -> bool:
    """At least one licence-OK hit with a thumbnail (the library: one clip found)."""
    if "hits" in src:
        return any(h.get("thumb_ok") and h.get("ok", True) for h in src["hits"])
    return bool(src.get("found"))
