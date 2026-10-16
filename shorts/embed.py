"""Local image/text embeddings (CLIP via open_clip) for searching scenes by description.

Runs on the laptop GPU; no Claude usage. ViT-B-32 needs well under 2 GB VRAM.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Protocol

import numpy as np

from .config import Settings

log = logging.getLogger(__name__)


class Encoder(Protocol):
    dim: int

    def images(self, paths: list[str]) -> np.ndarray: ...
    def texts(self, texts: list[str]) -> np.ndarray: ...


class ClipEncoder:
    def __init__(self, model: str, pretrained: str):
        import open_clip
        import torch

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.device == "cpu":
            log.warning("CUDA not available: embeddings run on CPU (slow). Check PyTorch install.")
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(model, pretrained=pretrained)
        self.model = self.model.to(self.device).eval()
        if self.device == "cuda":
            self.model = self.model.half()
        self.tokenizer = open_clip.get_tokenizer(model)
        self.dim = self.model.text_projection.shape[1] if hasattr(self.model, "text_projection") else 512

    def _norm(self, x) -> np.ndarray:
        x = x.float()
        x = x / x.norm(dim=-1, keepdim=True)
        return x.cpu().numpy().astype(np.float32)

    def images(self, paths: list[str]) -> np.ndarray:
        from PIL import Image

        batch = self.torch.stack([self.preprocess(Image.open(p).convert("RGB")) for p in paths]).to(self.device)
        if self.device == "cuda":
            batch = batch.half()
        with self.torch.no_grad():
            return self._norm(self.model.encode_image(batch))

    def texts(self, texts: list[str]) -> np.ndarray:
        tokens = self.tokenizer(texts).to(self.device)
        with self.torch.no_grad():
            return self._norm(self.model.encode_text(tokens))


_encoder: Encoder | None = None


def get_encoder(settings: Settings) -> Encoder:
    global _encoder
    if _encoder is None:
        _encoder = ClipEncoder(settings.clip_model, settings.clip_pretrained)
    return _encoder


def set_encoder(encoder: Encoder | None) -> None:
    """For tests: inject a fake encoder."""
    global _encoder
    _encoder = encoder


def embed_pending(conn: sqlite3.Connection, settings: Settings, batch_size: int = 64) -> int:
    enc = get_encoder(settings)
    rows = conn.execute(
        "SELECT id, keyframe FROM scenes WHERE embedding IS NULL AND rejected = 0 AND keyframe IS NOT NULL"
    ).fetchall()
    done = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        vecs = enc.images([r["keyframe"] for r in chunk])
        conn.executemany("UPDATE scenes SET embedding = ? WHERE id = ?",
                         [(v.astype(np.float32).tobytes(), r["id"]) for v, r in zip(vecs, chunk)])
        conn.commit()
        done += len(chunk)
        log.info("embedded %d/%d scenes", done, len(rows))
    if done:
        from .dedupe import reject_duplicate_scenes
        if dups := reject_duplicate_scenes(conn):
            log.info("%d scenes look the same as a scene from another video; they won't be reused", dups)
    return done


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def search(conn: sqlite3.Connection, settings: Settings, query: str, k: int = 10,
           exclude: set[int] | None = None, asset_id: int | None = None) -> list[tuple[int, float]]:
    """Best scenes for a text query: CLIP similarity plus a small boost when NASA's own
    title/keywords mention the query words. Returns [(scene_id, score)]."""
    sql = """SELECT sc.id, sc.embedding, a.title, a.keywords FROM scenes sc
              JOIN assets a ON a.id = sc.asset_id
             WHERE sc.embedding IS NOT NULL AND sc.rejected = 0"""
    params: tuple = ()
    if asset_id is not None:
        sql += " AND sc.asset_id = ?"
        params = (asset_id,)
    rows = conn.execute(sql, params).fetchall()
    exclude = exclude or set()
    rows = [r for r in rows if r["id"] not in exclude]
    if not rows:
        return []
    mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    q = get_encoder(settings).texts([query])[0]
    sims = mat @ q
    qw = _words(query)
    if qw:
        boost = np.array([len(qw & _words(f"{r['title'] or ''} {r['keywords'] or ''}")) / len(qw)
                          for r in rows], dtype=np.float32)
        sims = sims + 0.05 * boost
    order = np.argsort(-sims)[:k]
    return [(int(rows[i]["id"]), float(sims[i])) for i in order]
