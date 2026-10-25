"""Local image/text embeddings (open_clip) for searching scenes by description.

Two search spaces. 'clip': CLIP ViT-B-32, one keyframe per scene (duplicates, categories and hero
picking use it too). 'pe': Perception Encoder B/16 on 3 frames per scene, scored on the best
frame: the clip-quality research's winner (project files: research/clip-quality.md; first clip
clearly on topic 55% -> 78%). Search uses 'pe' once nearly the whole library has its vectors
(deploy/pe-embed.sh fills them once; new footage gets them when it is embedded).

Runs on the CPU; no Claude usage.
"""
from __future__ import annotations

import itertools
import logging
import re
import sqlite3
import threading
import time
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

    def arrays(self, images: list[np.ndarray], pad: bool = True) -> np.ndarray:
        """BGR images (OpenCV) of any shape, padded to a square with black first: CLIP's own
        preprocessing would cut a tall crop or a wide frame to its middle square.
        pad=False: the model's own preprocessing (whole video frames, as they were embedded)."""
        from PIL import Image

        def square(img: np.ndarray) -> Image.Image:
            if not pad:
                return Image.fromarray(np.ascontiguousarray(img[:, :, ::-1]))
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


CLIP_META = 0.05   # CLIP: bonus per share of the query's words found in the video's title/keywords
PE_SPOTS = (0.15, 0.5, 0.85)  # where in a scene its 3 Perception Encoder frames are taken
PE_META = 0.5      # PE: the same bonus in standard deviations of the query's scores (as the bench measured)
PE_READY = 0.9     # search switches to PE once this share of the searchable scenes has been through it
PE_REFRESH = 600   # while the background job adds vectors, the PE index is reloaded at most this often (s)

_encoder: Encoder | None = None
_encoder_lock = threading.Lock()  # make warms CLIP up in a thread: load it once, not twice
_pe: Encoder | None = None  # Perception Encoder B/16: picks a clip's best seconds (inpoint.py)


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


def get_pe(settings: Settings) -> Encoder:
    """Perception Encoder B/16 (Meta, Apache-2.0): the clip-quality research's best model for
    "does this frame show X" on our footage (research/clip-quality.md in the project files)."""
    global _pe
    if _pe is None:
        with _encoder_lock:
            if _pe is None:
                _pe = ClipEncoder(settings.pe_model, settings.pe_pretrained)
    return _pe


def set_pe(encoder: Encoder | None) -> None:
    """For tests: inject a fake encoder."""
    global _pe
    _pe = encoder


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
        if settings.search_pe:
            try:
                embed_pe(conn, settings, scene_ids=[r["id"] for r in rows])
            except Exception as e:  # noqa: BLE001 - CLIP search still works; deploy/pe-embed.sh catches up
                log.warning("new search: no vectors for the new scenes (%s)", e)
    return done


def _pe_frames(path: str, scenes: list[tuple[int, float, float]]) -> list[tuple[int, list[np.ndarray]]]:
    """The PE_SPOTS frames of each scene of one video (BGR, as wide as the keyframes)."""
    import cv2
    from .scenes import FRAME_W
    cap = cv2.VideoCapture(str(path or ""))
    out = []
    try:
        for sid, start, end in scenes:
            frames = []
            for f in PE_SPOTS:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(start + (end - start) * f, 0) * 1000)
                ok, img = cap.read()
                if ok and img is not None and img.size:
                    h, w = img.shape[:2]
                    frames.append(cv2.resize(img, (FRAME_W, max(1, round(h * FRAME_W / w))),
                                             interpolation=cv2.INTER_AREA))
            out.append((sid, frames))
    finally:
        cap.release()
    return out


def _pe_blob(vecs: list[np.ndarray]) -> bytes:
    """Always 3 rows (a missing frame repeats one that was read), float16; b'' = nothing readable."""
    if not len(vecs):
        return b""
    v = np.stack(vecs)
    v = np.concatenate([v, np.repeat(v[-1:], len(PE_SPOTS) - len(v), axis=0)])
    return v.astype(np.float16).tobytes()


def embed_pe(conn: sqlite3.Connection, settings: Settings, scene_ids: list[int] | None = None,
             limit: int | None = None, threads: int | None = None, unit: int = 64) -> int:
    """Perception Encoder vectors for the searchable scenes that have none. scene_ids: only these
    (new footage); None: the whole library (the one-time background job, deploy/pe-embed.sh).
    Frames are read a few videos ahead while the model embeds. Returns how many scenes."""
    rows = conn.execute(
        """SELECT sc.id, sc.start, sc.end, a.path FROM scenes sc JOIN assets a ON a.id = sc.asset_id
            WHERE sc.pe IS NULL AND sc.embedding IS NOT NULL AND sc.rejected = 0
            ORDER BY a.id, sc.start""").fetchall()
    if scene_ids is not None:
        want = set(scene_ids)
        rows = [r for r in rows if r["id"] in want]
    rows = rows[:limit] if limit else rows
    if not rows:
        return 0
    enc = get_pe(settings)
    if threads:
        import torch
        torch.set_num_threads(threads)
    units = []
    for path, group in itertools.groupby(rows, key=lambda r: r["path"]):
        group = [(r["id"], r["start"], r["end"]) for r in group]
        units += [(path, group[i:i + unit]) for i in range(0, len(group), unit)]
    from concurrent.futures import ThreadPoolExecutor
    done, t0 = 0, time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ahead = [pool.submit(_pe_frames, *u) for u in units[:3]]
        for nxt in itertools.count(3):
            if not ahead:
                break
            got = ahead.pop(0).result()
            if nxt < len(units):
                ahead.append(pool.submit(_pe_frames, *units[nxt]))
            items = [(sid, f) for sid, frames in got for f in frames]
            imgs = [f for _, f in items]
            vecs = [v for i in range(0, len(imgs), 32) for v in enc.arrays(imgs[i:i + 32], pad=False)]
            per: dict[int, list] = {}
            for (sid, _), v in zip(items, vecs):
                per.setdefault(sid, []).append(v)
            conn.executemany("UPDATE scenes SET pe = ? WHERE id = ?",
                             [(_pe_blob(per.get(sid, [])), sid) for sid, _ in got])
            conn.commit()
            done += len(got)
            progress.report(done, len(rows), "embed for the new search")
            if scene_ids is None:
                rate = done / (time.perf_counter() - t0)
                log.info("new search: %d/%d scenes, %.2f per second, about %.1f h to go",
                         done, len(rows), rate, (len(rows) - done) / rate / 3600)
    return done


def import_bench_pe(conn: sqlite3.Connection, settings: Settings) -> int:
    """The vectors the clip-quality bench already made of its test library (same model, same
    frames): a head start for the background job. Only for scenes the bench's copy of the database
    has with the same video and times."""
    folder = settings.data_dir / "bench"
    emb, snap = folder / "emb" / "pe-b16.npz", folder / "shorts.db"
    if not emb.exists() or not snap.exists():
        return 0
    with np.load(emb) as z:
        ids, img, valid = z["ids"], z["img"], z["valid"]
    s = sqlite3.connect(str(snap))
    try:
        then = {r[0]: r[1:] for r in s.execute("SELECT id, asset_id, start, end FROM scenes")}
    finally:
        s.close()
    now = {r[0]: tuple(r[1:]) for r in conn.execute(
        "SELECT id, asset_id, start, end FROM scenes WHERE pe IS NULL AND embedding IS NOT NULL AND rejected = 0")}
    rows = [(_pe_blob(list(v[ok])), int(sid)) for sid, v, ok in zip(ids, img, valid)
            if ok.any() and int(sid) in now and now[int(sid)] == then.get(int(sid))]
    conn.executemany("UPDATE scenes SET pe = ? WHERE id = ?", rows)
    conn.commit()
    return len(rows)


def _counts(conn: sqlite3.Connection) -> tuple:
    """Searchable scenes: count, max id, id sum (a cheap fingerprint), how many have been through
    the Perception Encoder, and how many of those have vectors."""
    return tuple(conn.execute(
        """SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(id), 0), COUNT(pe),
                  COALESCE(SUM(length(pe) > 0), 0) FROM scenes
            WHERE embedding IS NOT NULL AND rejected = 0""").fetchone())


def pe_status(conn: sqlite3.Connection) -> dict:
    n, _, _, tried, ready = _counts(conn)
    return {"scenes": n, "ready": ready, "unreadable": tried - ready, "share": tried / n if n else 0.0}


def search_space(conn: sqlite3.Connection, settings: Settings) -> str:
    """'pe' when the new search is on and nearly every searchable scene has been through it."""
    n, _, _, tried, ready = _counts(conn)
    return "pe" if settings.search_pe and ready and tried >= PE_READY * n else "clip"


def query_encoder(settings: Settings, space: str) -> Encoder:
    return get_pe(settings) if space == "pe" else get_encoder(settings)


def scene_vector(conn: sqlite3.Connection, space: str, scene_id: int) -> np.ndarray | None:
    """A scene's image vector in a search space (PE: the mean of its frames), normalised."""
    r = conn.execute(f"SELECT {'pe' if space == 'pe' else 'embedding'} FROM scenes WHERE id=?",
                     (scene_id,)).fetchone()
    if not r or not r[0]:
        return None
    if space == "pe":
        v = np.frombuffer(r[0], dtype=np.float16).astype(np.float32).reshape(len(PE_SPOTS), -1).mean(axis=0)
    else:
        v = np.frombuffer(r[0], dtype=np.float32)
    return v / (np.linalg.norm(v) or 1.0)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


class _Index:
    """All searchable scenes in memory, so a search doesn't re-read every embedding from SQLite.
    'pe': only scenes with Perception Encoder vectors, mat is (scenes, 3, dim)."""

    def __init__(self, conn: sqlite3.Connection, key: tuple, space: str, ready: int = 0):
        pe = space == "pe"
        rows = conn.execute(
            f"""SELECT sc.id, sc.asset_id, {'sc.pe' if pe else 'sc.embedding'} AS vec, a.title, a.keywords
                  FROM scenes sc JOIN assets a ON a.id = sc.asset_id
                 WHERE sc.embedding IS NOT NULL AND sc.rejected = 0{' AND length(sc.pe) > 0' if pe else ''}
                 ORDER BY sc.id""").fetchall()
        self.key, self.space, self.ready, self.built = key, space, ready, time.monotonic()
        self.ids = np.array([r["id"] for r in rows], dtype=np.int64)
        self.assets = np.array([r["asset_id"] for r in rows], dtype=np.int64)
        if not rows:
            self.mat = np.zeros((0, len(PE_SPOTS), 1) if pe else (0, 1), dtype=np.float32)
        elif pe:
            self.mat = np.stack([np.frombuffer(r["vec"], dtype=np.float16).reshape(len(PE_SPOTS), -1)
                                 for r in rows]).astype(np.float32)
        else:
            self.mat = np.stack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
        self.dim = self.mat.shape[-1]
        self.words = [_words(f"{r['title'] or ''} {r['keywords'] or ''}") for r in rows]
        self._last: tuple[bytes, np.ndarray] | None = None

    def scores(self, q: np.ndarray) -> np.ndarray:
        """Similarity of every scene to the query; PE: its best frame. The last query's answer is
        kept (shots.py searches every block twice: its own video, then the library)."""
        key = q.tobytes()
        if self._last is not None and self._last[0] == key:
            return self._last[1]
        if self.space == "pe":
            n, k, d = self.mat.shape
            sims = (self.mat.reshape(n * k, d) @ q).reshape(n, k).max(axis=1)
        else:
            sims = self.mat @ q
        self._last = (key, sims)
        return sims


_indexes: dict[str, _Index] = {}


def _get_index(conn: sqlite3.Connection, space: str = "clip") -> _Index:
    n, top, total, _, ready = _counts(conn)
    # Changes when scenes are embedded or rejected; the database file tells test databases apart.
    key = (n, top, total, id(conn), conn.execute("PRAGMA database_list").fetchone()[2])
    index = _indexes.get(space)
    if (index is None or index.key != key
            or (space == "pe" and index.ready != ready and time.monotonic() - index.built > PE_REFRESH)):
        index = _indexes[space] = _Index(conn, key, space, ready)
    return index


def search(conn: sqlite3.Connection, settings: Settings, query: str, k: int = 10,
           exclude: set[int] | None = None, asset_id: int | None = None,
           allowed: set[int] | None = None, qvec: np.ndarray | None = None,
           space: str | None = None) -> list[tuple[int, float]]:
    """Best scenes for a text query: image-text similarity plus a small boost when the archive's
    own title/keywords mention the query words. allowed: only these scenes (a channel's
    categories). space: 'clip' or 'pe' (default: search_space()); qvec: the query's vector from
    that space's encoder (query_encoder()) when the caller already has it. Returns [(scene_id, score)]."""
    space = space or search_space(conn, settings)
    index = _get_index(conn, space)
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
    q = qvec if qvec is not None and qvec.shape[-1] == index.dim else query_encoder(settings, space).texts([query])[0]
    every = index.scores(q)
    sims = every[sel]
    qw = _words(query)
    if qw:
        boost = np.array([len(qw & index.words[i]) / len(qw) for i in sel], dtype=np.float32)
        # PE scores spread differently per query: the bonus is in standard deviations, as measured.
        sims = sims + (PE_META * float(every.std()) if space == "pe" else CLIP_META) * boost
    order = np.argsort(-sims)[:k]
    return [(int(index.ids[sel[i]]), float(sims[i])) for i in order]
