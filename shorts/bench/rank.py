"""Every search method ranks the whole bench library for every block (top 20 kept).

A method = a model + how a scene's frames count (middle frame only, best of 3, average of 3)
+ the archive title/keyword boost the pipeline already uses (+meta) + optionally the narration
of the original film (+asr). "today" is exactly what shots.py does now: CLIP ViT-B-32 on the
middle frame plus 0.05 x the share of query words found in the video's title and keywords."""
from __future__ import annotations

import logging
import re

import numpy as np

from . import load, root, save
from .models import MODELS, load_emb

log = logging.getLogger(__name__)

TOP = 20
META_Z = 0.5   # title/keyword match worth half a standard deviation of the model's scores
ASR_Z = 1.0    # narration match worth one


def words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) > 2:
            out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def overlap(query: str, texts: list[str]) -> np.ndarray:
    q = words(query)
    if not q:
        return np.zeros(len(texts), np.float32)
    return np.array([len(q & words(t)) / len(q) for t in texts], np.float32)


def scene_scores(e: dict, mode: str) -> np.ndarray:
    """(scenes, blocks) similarity for one way of counting a scene's frames."""
    img = e["img"].astype(np.float32)
    valid = e["valid"]
    q = e["queries"].astype(np.float32)
    if mode == "mid":
        return img[:, 1] @ q.T
    sims = np.einsum("skd,bd->skb", img, q)
    sims = np.where(valid[:, :, None], sims, -np.inf if mode == "max3" else np.nan)
    if mode == "max3":
        out = sims.max(axis=1)
    else:
        out = np.nanmean(sims, axis=1)
    return np.nan_to_num(out, nan=-1.0, neginf=-1.0)


def _z(s: np.ndarray) -> np.ndarray:
    return (s - s.mean(axis=0, keepdims=True)) / (s.std(axis=0, keepdims=True) + 1e-6)


def run(settings, ts: dict) -> dict:
    out_path = root(settings) / "rank.json"
    ranks = load(out_path, {})
    blocks = ts["blocks"]
    asr = load(root(settings) / "asr.json", {})
    for name in MODELS:
        e = load_emb(settings, name)
        if e is None:
            continue
        ids = e["ids"]
        scenes = [ts["scenes"][str(i)] for i in ids]
        meta_text = [f"{s.get('title') or ''} {s.get('keywords') or ''}" for s in scenes]
        meta = np.stack([overlap(b["query"], meta_text) for b in blocks], axis=1) if blocks else None
        asr_text = [asr.get(str(i), "") for i in ids]
        asr_m = (np.stack([overlap(b["query"], asr_text) for b in blocks], axis=1)
                 if blocks and any(asr_text) else None)
        three = bool(e["valid"][:, [0, 2]].any())
        for mode in ("mid", "max3", "mean3") if three else ("mid",):
            raw = scene_scores(e, mode)
            variants = {f"{name}/{mode}": _z(raw), f"{name}/{mode}+meta": _z(raw) + META_Z * meta}
            if name == "clip-b32" and mode == "mid":
                variants["today"] = raw + 0.05 * meta
            if asr_m is not None:
                variants[f"{name}/{mode}+meta+asr"] = _z(raw) + META_Z * meta + ASR_Z * asr_m
            for method, s in variants.items():
                ranks[method] = {}
                for bi, b in enumerate(blocks):
                    order = np.argsort(-s[:, bi])[:TOP]
                    ranks[method][str(b["id"])] = [[int(ids[o]), float(s[o, bi])] for o in order]
    # What the pipeline really showed: Claude's pick from today's contact-sheet menu (top 1 only).
    ranks["pipeline-now"] = {str(b["id"]): [[b["picked"], 0.0]] for b in blocks
                             if b["picked"] and str(b["picked"]) in ts["scenes"]}
    save(out_path, ranks)
    return ranks


def pool(ts: dict, ranks: dict, methods: list[str], size: int = 30) -> dict[str, list[int]]:
    """Per block the candidates Claude labels: rank 1 of every method, then rank 2, ... until
    `size`, plus the clip the pipeline actually used."""
    out = {}
    for b in ts["blocks"]:
        key = str(b["id"])
        got: list[int] = []
        if b["picked"] and str(b["picked"]) in ts["scenes"]:
            got.append(b["picked"])
        for depth in range(TOP):
            for m in methods:
                lst = ranks.get(m, {}).get(key, [])
                if depth < len(lst) and lst[depth][0] not in got:
                    got.append(lst[depth][0])
            if len(got) >= size:
                break
        out[key] = got[:size]
    return out
