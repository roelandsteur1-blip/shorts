"""Sort footage into categories (space, war, history, ...) per scene, so a channel only uses scenes
from its own categories, even when one archive video mixes subjects (a 1950s newsreel with a rocket
launch, then a parade).

Three steps, cheapest first; each result is stored with a confidence and the method:
1. source metadata: the archive's own title, subjects and description against category keywords;
2. CLIP (local): each scene's embedding against the category's clip_prompts, plus the scenes
   Roeland sorted by hand as extra examples;
3. Claude, only when 1 and 2 disagree or are both unsure: one call sorts up to 50 videos.
Anything still unsure stays uncategorised, for sorting by hand on the Library page.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import embed, llm, sources
from .config import Settings

log = logging.getLogger(__name__)

META_SURE = 0.6      # metadata confidence that counts as sure (about two keyword hits)
META_SOME = 0.3      # one weak hit: enough to agree with CLIP, not enough alone
CLIP_SURE = 0.6      # CLIP probability that counts as sure
CLIP_AGREE = 0.15    # CLIP probability that is enough to confirm a metadata category
CLAUDE_BATCH = 50


@dataclass
class Category:
    id: str
    label: str
    keywords: list[str] = field(default_factory=list)
    clip_prompts: list[str] = field(default_factory=list)
    search: dict = field(default_factory=dict)

    def queries(self, source: str) -> list[str]:
        """Search hints for restocking this category from one source (fallback: top keywords)."""
        hint = self.search.get(source)
        if hint:
            return [hint] if isinstance(hint, str) else list(hint)
        return self.keywords[:3]


def categories_path(settings: Settings) -> Path:
    return settings.config_dir / "categories.toml"


def load_categories(settings: Settings) -> list[Category]:
    path = categories_path(settings)
    if not path.exists():
        return []
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return [Category(**c) for c in data.get("category", [])]


# ---------- step 1: metadata ----------

def _phrase_re(phrase: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])")


def metadata_scores(asset: sqlite3.Row | dict, cats: list[Category]) -> dict[str, float]:
    """Keyword hits in title/subjects count 1, in the description 0.5; 2 hits = sure."""
    head = f"{asset['title'] or ''} {asset['keywords'] or ''}".lower()
    body = (asset["description"] or "").lower()
    out = {}
    for c in cats:
        score = 0.0
        for kw in c.keywords:
            rx = _phrase_re(kw)
            if rx.search(head):
                score += 1
            elif rx.search(body):
                score += 0.5
        if score:
            out[c.id] = min(1.0, 0.45 * score)
    return out


# ---------- step 2: CLIP ----------

def clip_probs(conn: sqlite3.Connection, settings: Settings, emb: np.ndarray, cats: list[Category]) -> np.ndarray:
    """(scenes x categories) probabilities: CLIP zero-shot against the prompts."""
    enc = embed.get_encoder(settings)
    sims = np.full((len(emb), len(cats)), -1.0, dtype=np.float32)
    for j, c in enumerate(cats):
        prompts = c.clip_prompts or [c.label]
        sims[:, j] = (emb @ enc.texts(prompts).T).max(axis=1)
    logits = 100.0 * sims
    p = np.exp(logits - logits.max(axis=1, keepdims=True))
    p /= p.sum(axis=1, keepdims=True)
    return p


# ---------- step 3: Claude ----------

PROMPT = """Sort archive videos into footage categories for YouTube Shorts channels.

Categories (id: label):
{categories}

Videos:
{items}

For each video, list every category its footage clearly belongs to (a WWII bomber film is both
"war" and "aviation"; a newsreel can cover several). Use an empty list if none fits.
Answer with only JSON: [{{"n": <video number>, "categories": ["id", ...]}}]
"""


def ask_claude(settings: Settings, assets: list[sqlite3.Row], cats: list[Category]) -> dict[int, list[str]]:
    known = {c.id for c in cats}
    out: dict[int, list[str]] = {}
    for i in range(0, len(assets), CLAUDE_BATCH):
        batch = assets[i:i + CLAUDE_BATCH]
        items = "\n".join(
            f"{n + 1}. {a['title'] or ''} | subjects: {(a['keywords'] or '')[:200]} | "
            f"{(a['description'] or '')[:300]}" for n, a in enumerate(batch))
        prompt = PROMPT.format(categories="\n".join(f"{c.id}: {c.label}" for c in cats), items=items)
        reply = llm.ask_json(prompt, settings.claude_cmd, stage="claude categorize")
        for r in reply if isinstance(reply, list) else []:
            try:
                n = int(r.get("n"))
            except (TypeError, ValueError, AttributeError):
                continue
            if 1 <= n <= len(batch):
                out[batch[n - 1]["id"]] = [c for c in r.get("categories") or [] if c in known]
    return out


# ---------- sorting ----------

def _decide(meta: dict[str, float], p: np.ndarray, cats: list[Category]) -> tuple[dict, bool]:
    """Categories for one scene from steps 1 and 2: ({cat: (confidence, method)}, needs_claude)."""
    ids = [c.id for c in cats]
    top = int(np.argmax(p))
    agreed = {c: ((m + float(p[ids.index(c)])) / 2, "metadata+clip")
              for c, m in meta.items() if m >= META_SOME and p[ids.index(c)] >= CLIP_AGREE}
    if agreed:
        return agreed, False
    sure_meta = {c: m for c, m in meta.items() if m >= META_SURE}
    clip_sure = p[top] >= CLIP_SURE
    if clip_sure and not sure_meta:
        return {ids[top]: (float(p[top]), "clip")}, False
    if sure_meta and not clip_sure:
        return {c: (0.8 * m, "metadata") for c, m in sure_meta.items()}, False
    return {}, True  # disagree, or both unsure


def _claude_pick(cands: list[str], p: np.ndarray, cats: list[Category]) -> dict:
    if not cands:
        return {}
    if len(cands) == 1:
        return {cands[0]: (0.7, "claude")}
    ids = [c.id for c in cats]
    sub = np.array([p[ids.index(c)] for c in cands])
    sub = sub / sub.sum() if sub.sum() > 0 else np.full(len(cands), 1 / len(cands))
    best = int(np.argmax(sub))
    return {c: (0.6, "claude+clip") for i, c in enumerate(cands) if i == best or sub[i] >= 0.4}


def _store(conn: sqlite3.Connection, scene_id: int, found: dict) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO scene_categories (scene_id, category, confidence, method) VALUES (?,?,?,?)",
        [(scene_id, c, round(conf, 3), method) for c, (conf, method) in found.items()])
    conn.execute("UPDATE scenes SET sorted = 1 WHERE id = ?", (scene_id,))


def sort_pending(conn: sqlite3.Connection, settings: Settings, use_claude: bool = True,
                 fixed_only: bool = False) -> dict:
    """Sort every usable scene that hasn't been sorted yet. Returns counts per method.
    fixed_only: only sources that are one subject (NASA); instant, no CLIP or Claude."""
    cats = load_categories(settings)
    counts: dict[str, int] = {}
    if not cats:
        return counts
    known = {c.id for c in cats}
    rows = conn.execute(
        """SELECT sc.id, sc.asset_id, sc.embedding, a.source, a.title, a.keywords, a.description
             FROM scenes sc JOIN assets a ON a.id = sc.asset_id
            WHERE sc.sorted = 0 AND sc.rejected = 0 ORDER BY sc.asset_id, sc.id""").fetchall()
    # Sources that are one subject only (NASA: space) need no sorting.
    rest = []
    for r in rows:
        fixed = [c for c in sources.fixed_categories(r["source"]) if c in known]
        if fixed:
            _store(conn, r["id"], {c: (1.0, "source") for c in fixed})
            counts["source"] = counts.get("source", 0) + 1
        elif r["embedding"] is not None:
            rest.append(r)
    conn.commit()
    if not rest or fixed_only:
        _rollup(conn)
        return counts

    emb = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rest])
    probs = clip_probs(conn, settings, emb, cats)
    meta_cache: dict[int, dict] = {}
    unsure: dict[int, list[int]] = {}  # asset id -> row indexes for Claude
    for i, r in enumerate(rest):
        meta = meta_cache.setdefault(r["asset_id"], metadata_scores(r, cats))
        found, needs_claude = _decide(meta, probs[i], cats)
        if needs_claude:
            unsure.setdefault(r["asset_id"], []).append(i)
            continue
        _store(conn, r["id"], found)
        for _, method in found.values():
            counts[method] = counts.get(method, 0) + 1
    conn.commit()

    if unsure and use_claude:
        assets = conn.execute(f"SELECT * FROM assets WHERE id IN ({','.join('?' * len(unsure))})",
                              list(unsure)).fetchall()
        try:
            picks = ask_claude(settings, assets, cats)
        except llm.LLMError as e:
            log.warning("Claude could not sort %d videos (%s); they stay unsorted and are retried later",
                        len(unsure), e)
            picks = None
        if picks is not None:
            for aid, idxs in unsure.items():
                for i in idxs:
                    found = _claude_pick(picks.get(aid, []), probs[i], cats)
                    _store(conn, rest[i]["id"], found)
                    key = "claude" if found else "uncategorised"
                    counts[key] = counts.get(key, 0) + 1
            conn.commit()
    _rollup(conn)
    return counts


def resort(conn: sqlite3.Connection) -> None:
    """Forget every result, e.g. after editing categories.toml."""
    conn.execute("DELETE FROM scene_categories")
    conn.execute("UPDATE scenes SET sorted = 0")
    conn.commit()


def _rollup(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM asset_categories")
    conn.execute("""INSERT INTO asset_categories (asset_id, category, scenes)
                    SELECT sc.asset_id, c.category, COUNT(*) FROM scene_categories c
                      JOIN scenes sc ON sc.id = c.scene_id
                     WHERE c.confidence > 0 AND sc.rejected = 0 GROUP BY sc.asset_id, c.category""")
    conn.commit()


def scene_filter(categories: list[str] | None, alias: str = "sc") -> tuple[str, list]:
    """SQL condition (and its parameters) limiting scenes to these categories; none = no limit."""
    if not categories:
        return "1=1", []
    marks = ",".join("?" * len(categories))
    return (f"{alias}.id IN (SELECT scene_id FROM scene_categories WHERE confidence > 0 "
            f"AND category IN ({marks}))", list(categories))


def allowed_scenes(conn: sqlite3.Connection, categories: list[str] | None) -> set[int] | None:
    if not categories:
        return None
    cond, args = scene_filter(categories)
    return {r[0] for r in conn.execute(f"SELECT sc.id FROM scenes sc WHERE {cond}", args)}
