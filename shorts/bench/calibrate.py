"""The minimum match for the new search, read off the judge's labels.

shots.py keeps a block off a clip from another video when the clip's search score is under
MIN_MATCH (CLIP: 0.22), so a poor match never beats staying in the hero's own video. Perception
Encoder scores sit on another scale, so its threshold comes from the bench: the PE score that lets
through the same share of clearly on-topic clips as CLIP's 0.22 does (no fewer good clips used),
and how many off-topic clips each one lets through. Scores are computed the way embed.search
computes them in production. Writes calibrate.json."""
from __future__ import annotations

import numpy as np

from .. import embed, shots
from . import load, root, save
from .models import load_emb


def _overlap(query: str, texts: list[str]) -> np.ndarray:
    q = embed._words(query)
    if not q:
        return np.zeros(len(texts), np.float32)
    return np.array([len(q & embed._words(t)) / len(q) for t in texts], np.float32)


def production_scores(e: dict, ts: dict, space: str) -> tuple[dict, list[float]]:
    """{(block id, scene id): score} for every block and scene, and each query's score spread."""
    ids = [int(i) for i in e["ids"]]
    img = e["img"].astype(np.float32)
    valid = e["valid"]
    meta_text = [f"{ts['scenes'][str(i)].get('title') or ''} {ts['scenes'][str(i)].get('keywords') or ''}"
                 for i in ids]
    by_block = {int(b): i for i, b in enumerate(e["block_ids"])}
    out, spreads = {}, []
    for b in ts["blocks"]:
        bi = by_block.get(int(b["id"]))
        if bi is None:
            continue
        q = e["queries"][bi].astype(np.float32)
        if space == "pe":
            sims = np.where(valid, np.einsum("skd,d->sk", img, q), -np.inf).max(axis=1)
            sims = np.where(np.isfinite(sims), sims, -1.0)
            spread = float(sims.std())
            score = sims + embed.PE_META * spread * _overlap(b["query"], meta_text)
        else:
            sims = img[:, 1] @ q
            spread = float(sims.std())
            score = sims + embed.CLIP_META * _overlap(b["query"], meta_text)
        spreads.append(spread)
        for sid, s in zip(ids, score):
            out[(int(b["id"]), sid)] = float(s)
    return out, spreads


def _rates(pairs: list[tuple[float, int]], thr: float) -> dict:
    def share(topic):
        vals = [s >= thr for s, t in pairs if t == topic]
        return round(float(np.mean(vals)), 3) if vals else None
    return {"threshold": round(thr, 4), "on_topic_kept": share(2), "related_kept": share(1),
            "off_topic_kept": share(0)}


def _youden(pairs: list[tuple[float, int]]) -> float:
    pos = np.array([s for s, t in pairs if t == 2])
    neg = np.array([s for s, t in pairs if t == 0])
    best, best_v = 0.0, -1.0
    for thr in np.unique(np.concatenate([pos, neg])):
        v = float((pos >= thr).mean() - (neg >= thr).mean())
        if v > best_v:
            best, best_v = float(thr), v
    return best


def run(settings, ts: dict) -> dict:
    labels = load(root(settings) / "judge.json", {})
    out = {}
    pairs_by = {}
    for name, space in (("clip-b32", "clip"), ("pe-b16", "pe")):
        e = load_emb(settings, name)
        if e is None:
            continue
        scores, spreads = production_scores(e, ts, space)
        pairs = [(scores[(int(b), int(s))], int(v[0])) for b, got in labels.items() for s, v in got.items()
                 if (int(b), int(s)) in scores]
        if not pairs:
            continue
        pairs_by[space] = pairs
        out[space] = {"pairs": len(pairs), "median_spread": round(float(np.median(spreads)), 4),
                      "youden": _rates(pairs, _youden(pairs))}
    if "clip" in out:
        out["clip"]["today"] = _rates(pairs_by["clip"], shots.MIN_MATCH)
    if "clip" in out and "pe" in out:
        keep = out["clip"]["today"]["on_topic_kept"] or 0.0
        pos = np.sort([s for s, t in pairs_by["pe"] if t == 2])
        thr = float(np.quantile(pos, 1 - keep)) if len(pos) else 0.0
        out["pe"]["same_on_topic_kept"] = _rates(pairs_by["pe"], thr)
        out["bonus_scale"] = round(out["pe"]["median_spread"] / out["clip"]["median_spread"], 3)
    save(root(settings) / "calibrate.json", out)
    for space, v in out.items():
        print(space, v)
    return out
