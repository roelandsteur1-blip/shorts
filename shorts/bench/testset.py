"""The fixed test set: real b-roll blocks from real scripts, and a library of scenes to search.

Blocks are spread evenly over the channels (newest Shorts first, at most 2 blocks per Short).
The library holds every scene of the blocks' own videos, the scene the pipeline actually used
for each block, and random other videos until it reaches the target size, so a method has to
find the right clip among thousands of wrong ones, as in the real library."""
from __future__ import annotations

import logging
import math
import random
from collections import defaultdict
from pathlib import Path

from . import SEED, load, root, save, snapshot

log = logging.getLogger(__name__)


def build(settings, blocks: int = 80, library: int = 6000) -> dict:
    path = root(settings) / "testset.json"
    if (ts := load(path)) is not None:
        return ts
    conn = snapshot(settings)
    rng = random.Random(SEED)
    rows = conn.execute(
        """SELECT b.id, b.short_id, b.idx, b.text, b.visual, b.seconds, b.scene_id, s.channel, s.topic,
                  hs.asset_id AS hero_asset
             FROM blocks b JOIN shorts s ON s.id = b.short_id
             JOIN scenes hs ON hs.id = s.hero_scene_id
            WHERE b.idx > 0 AND b.visual IS NOT NULL AND TRIM(b.visual) NOT IN ('', 'hero')
                  AND s.status != 'planned'
            ORDER BY s.id DESC, b.idx""").fetchall()
    by_channel: dict[str, list] = defaultdict(list)
    for r in rows:
        by_channel[r["channel"]].append(r)
    per = math.ceil(blocks / max(len(by_channel), 1))
    chosen = []
    for ch, rs in sorted(by_channel.items()):
        per_short: dict[int, int] = defaultdict(int)
        recent = rs[:per * 4]
        rng.shuffle(recent)
        for r in recent:
            if per_short[r["short_id"]] >= 2:
                continue
            per_short[r["short_id"]] += 1
            chosen.append(r)
            if sum(per_short.values()) >= per:
                break
    chosen = chosen[:blocks] if len(chosen) > blocks else chosen

    def usable_assets(where: str = "", args=()):
        return conn.execute(
            f"""SELECT a.id, a.path, (SELECT COUNT(*) FROM scenes sc WHERE sc.asset_id = a.id AND sc.rejected = 0
                                       AND sc.keyframe IS NOT NULL) AS n
                  FROM assets a WHERE a.path IS NOT NULL AND a.needs_check = 0 AND a.claimed IS NULL
                  {where}""", args).fetchall()

    must = {r["hero_asset"] for r in chosen}
    must |= {r[0] for r in conn.execute(
        f"SELECT asset_id FROM scenes WHERE id IN ({','.join('?' * len(chosen))})",
        [r["scene_id"] for r in chosen if r["scene_id"]] or [-1])} if chosen else set()
    assets = [a for a in usable_assets() if a["n"] and Path(a["path"]).exists()]
    picked = [a for a in assets if a["id"] in must]
    others = [a for a in assets if a["id"] not in must]
    rng.shuffle(others)
    total = sum(a["n"] for a in picked)
    for a in others:
        if total >= library:
            break
        picked.append(a)
        total += a["n"]
    ids = [a["id"] for a in picked]
    scenes = conn.execute(
        f"""SELECT sc.id, sc.asset_id, sc.start, sc.end, sc.keyframe, sc.motion, sc.contrast, sc.brightness,
                   sc.text_like, a.path, a.width, a.height, a.title, a.keywords, a.source
              FROM scenes sc JOIN assets a ON a.id = sc.asset_id
             WHERE sc.asset_id IN ({','.join('?' * len(ids))}) AND sc.rejected = 0 AND sc.keyframe IS NOT NULL
                   AND sc.end - sc.start >= 1.0 ORDER BY sc.id""", ids).fetchall() if ids else []
    ts = {
        "blocks": [{"id": r["id"], "short_id": r["short_id"], "channel": r["channel"], "text": r["text"],
                    "visual": r["visual"], "topic": r["topic"], "seconds": r["seconds"],
                    "query": f"{r['visual'] or ''}, {r['topic'] or ''}".strip(", "),
                    "picked": r["scene_id"], "hero_asset": r["hero_asset"]} for r in chosen],
        "scenes": {str(s["id"]): dict(s) for s in scenes},
    }
    save(path, ts)
    log.info("test set: %d blocks over %d channels, library %d scenes from %d videos",
             len(ts["blocks"]), len(by_channel), len(scenes), len(ids))
    return ts
