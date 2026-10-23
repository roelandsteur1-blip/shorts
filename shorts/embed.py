"""Local image/text embeddings (CLIP via open_clip) for searching scenes by description.

Runs on the CPU; no Claude usage.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from typing import Protocol

import numpy as np

from . import progress
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
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(model, pretrained=pretrained)
        self.model = self.model.eval()
        self.tokenizer = open_clip.get_tokenizer(model)
        self.dim = self.model.text_projection.shape[1] if hasattr(self.model, "text_projection") else 512

    def _norm(self, x) -> np.ndarray:
        x = x.float()
        x = x / x.norm(dim=-1, keepdim=True)
        return x.cpu().numpy().astype(np.float32)

    def images(self, paths: list[str]) -> np.ndarray:
        from PIL import Image

        batch = self.torch.stack([self.preprocess(Image.open(p).convert("RGB")) for p in paths])
        with self.torch.no_grad():
            return self._norm(self.model.encode_image(batch))

    def arrays(self, images: list[np.ndarray]) -> np.ndarray:
        """BGR images (OpenCV) of any shape, padded to a square with black first: CLIP's own
        preprocessing would cut a tall crop or a wide frame to its middle square."""
        from PIL import Image

        def square(img: np.ndarray) -> Image.Image:
            h, w = img.shape[:2]
            n = max(h, w)
            out = np.zeros((n, n, 3), np.uint8)
            out[(n - h) // 2:(n - h) // 2 + h, (n - w) // 2:(n - w) // 2 + w] = img[:, :, ::-1]
            return Image.fromarray(out)

        batch = self.torch.stack([self.preprocess(square(i)) for i in images])
        with self.torch.no_grad():
            return self._norm(self.model.encode_image(batch))

    def texts(self, texts: list[str]) -> np.ndarray:
        tokens = self.tokenizer(texts)
        with self.torch.no_grad():
            return self._norm(self.model.encode_text(tokens))


_encoder: Encoder | None = None
_encoder_lock = threading.Lock()  # make warms CLIP up in a thread: load it once, not twice


def get_encoder(settings: Settings) -> Encoder:
    global _encoder
    if _encoder is None:
        with _encoder_lock:
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
        progress.report(done, len(rows), "embed")
    if done:
        from .dedupe import reject_duplicate_scenes
        if dups := reject_duplicate_scenes(conn):
            log.info("%d scenes look the same as a scene from another video; they won't be reused", dups)
    return done


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


class _Index:
    """All searchable scenes in memory, so a search doesn't re-read every embedding from SQLite."""

    def __init__(self, conn: sqlite3.Connection, key: tuple):
        rows = conn.execute(
            """SELECT sc.id, sc.asset_id, sc.embedding, a.title, a.keywords FROM scenes sc
                 JOIN assets a ON a.id = sc.asset_id
                WHERE sc.embedding IS NOT NULL AND sc.rejected = 0 ORDER BY sc.id""").fetchall()
        self.key = key
        self.ids = np.array([r["id"] for r in rows], dtype=np.int64)
        self.assets = np.array([r["asset_id"] for r in rows], dtype=np.int64)
        self.mat = (np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                    if rows else np.zeros((0, 1), dtype=np.float32))
        self.words = [_words(f"{r['title'] or ''} {r['keywords'] or ''}") for r in rows]


_index: _Index | None = None


def _get_index(conn: sqlite3.Connection) -> _Index:
    global _index
    # Cheap fingerprint of the searchable set; changes when scenes are embedded or rejected.
    key = tuple(conn.execute(
        """SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(id), 0) FROM scenes
            WHERE embedding IS NOT NULL AND rejected = 0""").fetchone()) + (
        id(conn), conn.execute("PRAGMA database_list").fetchone()[2])
    if _index is None or _index.key != key:
        _index = _Index(conn, key)
    return _index


def search(conn: sqlite3.Connection, settings: Settings, query: str, k: int = 10,
           exclude: set[int] | None = None, asset_id: int | None = None,
           allowed: set[int] | None = None, qvec: np.ndarray | None = None) -> list[tuple[int, float]]:
    """Best scenes for a text query: CLIP similarity plus a small boost when the archive's own
    title/keywords mention the query words. allowed: only these scenes (a channel's categories).
    qvec: the query's CLIP vector when the caller already has it (one CLIP call for many queries).
    Returns [(scene_id, score)]."""
    index = _get_index(conn)
    mask = np.ones(len(index.ids), dtype=bool)
    if asset_id is not None:
        mask &= index.assets == asset_id
    if allowed is not None:
        mask &= np.isin(index.ids, np.fromiter(allowed, dtype=np.int64, count=len(allowed)))
    if exclude:
        mask &= ~np.isin(index.ids, np.fromiter(exclude, dtype=np.int64))
    sel = np.where(mask)[0]
    if not len(sel):
        return []
    q = qvec if qvec is not None else get_encoder(settings).texts([query])[0]
    sims = index.mat[sel] @ q
    qw = _words(query)
    if qw:
        boost = np.array([len(qw & index.words[i]) / len(qw) for i in sel], dtype=np.float32)
        sims = sims + 0.05 * boost
    order = np.argsort(-sims)[:k]
    return [(int(index.ids[sel[i]]), float(sims[i])) for i in order]
