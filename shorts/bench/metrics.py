"""Scores for the report. Plain numpy, no Claude, no models."""
from __future__ import annotations

import numpy as np


def topic_scores(ranks: dict[str, list], labels: dict[str, dict]) -> dict:
    """One method's rankings vs the judge's topic labels (2 on topic, 1 related, 0 not).

    p1: share of blocks whose first clip is on topic; p1_any: first clip at least related;
    hit5: an on-topic clip in the first 5 (what a menu needs); ndcg5: order quality of the first
    5; covered: share of the first 5 the judge labelled (low = the numbers are less sure)."""
    p1, p1_any, hit5, ndcg, cov = [], [], [], [], []
    for key, lst in ranks.items():
        lab = labels.get(key)
        if not lab or not lst:
            continue
        top = [str(s) for s, _ in lst[:5]]
        got = [lab.get(s, [None])[0] for s in top]
        if got[0] is None:
            continue
        p1.append(got[0] == 2)
        p1_any.append(got[0] >= 1)
        known = [g for g in got if g is not None]
        cov.append(len(known) / len(top))
        hit5.append(any(g == 2 for g in known))
        gains = [(g or 0) for g in got]
        dcg = sum(g / np.log2(i + 2) for i, g in enumerate(gains))
        ideal = sorted((v[0] for v in lab.values()), reverse=True)[:5]
        idcg = sum(g / np.log2(i + 2) for i, g in enumerate(ideal))
        ndcg.append(dcg / idcg if idcg else 0.0)
    n = len(p1)
    mean = lambda v: float(np.mean(v)) if v else float("nan")  # noqa: E731
    return {"n": n, "p1": mean(p1), "p1_any": mean(p1_any), "hit5": mean(hit5), "ndcg5": mean(ndcg),
            "covered": mean(cov)}


def ranks_avg(v: np.ndarray) -> np.ndarray:
    """Ranks with ties sharing their average rank."""
    order = np.argsort(v, kind="mergesort")
    r = np.empty(len(v), float)
    r[order] = np.arange(len(v), dtype=float)
    vals = v[order]
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and vals[j + 1] == vals[i]:
            j += 1
        r[order[i:j + 1]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return float("nan")
    ra, rb = ranks_avg(a), ranks_avg(b)
    return float(np.corrcoef(ra, rb)[0, 1])


def auc(pos, neg) -> float:
    """Chance that a random on-topic clip scores above a random off-topic one."""
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    if not len(pos) or not len(neg):
        return float("nan")
    r = ranks_avg(np.concatenate([pos, neg]))
    return float((r[:len(pos)].sum() - len(pos) * (len(pos) - 1) / 2) / (len(pos) * len(neg)))


def window_scores(times: list[float], start: float, seconds: float, frame_labels: dict[str, int],
                  best_start: int | None) -> dict:
    """How much of a chosen window shows the subject (judge per frame, 0-2), and how far its
    start is from the judge's best start."""
    inside = [i for i, t in enumerate(times) if start - 1e-6 <= t < start + seconds - 1e-6]
    vals = [frame_labels.get(str(i + 1)) for i in inside]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {}
    out = {"in_view": float(np.mean(vals)) / 2, "all_clear": float(all(v == 2 for v in vals))}
    if isinstance(best_start, int) and 1 <= best_start <= len(times):
        out["off_by"] = abs(times[best_start - 1] - start)
    return out
