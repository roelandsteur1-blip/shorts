"""The image-text models under test, and the embeddings each makes of the test set.

All run through open_clip on the CPU. Each model embeds the 3 frames of every library scene with
its own preprocessing; the time per image is measured on the server itself, at the thread count
the bench runs with, so the report's CPU cost is what production would pay."""
from __future__ import annotations

import logging
import os
import resource
import time

import cv2
import numpy as np

from . import root, status_line
from .frames import SPOTS, load_frames

log = logging.getLogger(__name__)

# name: (open_clip model, pretrained weights, plain words for the report).
MODELS = {
    "clip-b32": ("ViT-B-32", "laion2b_s34b_b79k", "CLIP ViT-B-32 (today)"),
    "siglip2-b32": ("ViT-B-32-SigLIP2-256", "webli", "SigLIP 2 B/32 256px"),
    "siglip2-b16": ("ViT-B-16-SigLIP2-256", "webli", "SigLIP 2 B/16 256px"),
    "pe-b16": ("PE-Core-B-16", "meta", "Perception Encoder B/16"),
    "siglip2-so400m": ("ViT-SO400M-16-SigLIP2-384", "webli", "SigLIP 2 So400m 384px"),
    "pe-l14": ("PE-Core-L-14-336", "meta", "Perception Encoder L/14 336px"),
}
# The big models are 10-20x slower: they embed only the middle frame (3 frames would take a day).
# Whether 3 frames beat 1 is measured on the small models.
MIDDLE_ONLY = {"siglip2-so400m", "pe-l14"}

# Zero-shot "is this worth watching" prompts (approach V7), scored with every model.
INTERESTING = ["a breathtaking, dramatic shot with one clear subject",
               "a striking close-up full of motion and detail"]
BORING = ["a dull, static, empty shot with nothing happening",
          "a blurry, cluttered, low quality frame"]


class Encoder:
    def __init__(self, name: str, threads: int | None = None):
        import open_clip
        import torch

        self.torch = torch
        if threads:
            torch.set_num_threads(threads)
        model, pretrained, _ = MODELS[name]
        t0 = time.perf_counter()
        # BENCH_NO_WEIGHTS=1: random weights, same compute (trying the code offline)
        kw = {"pretrained": pretrained} if pretrained and not os.environ.get("BENCH_NO_WEIGHTS") else {}
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(model, **kw)
        self.model = self.model.eval()
        self.tokenizer = open_clip.get_tokenizer(model)
        self.load_seconds = time.perf_counter() - t0

    def _norm(self, x) -> np.ndarray:
        x = x.float()
        return (x / x.norm(dim=-1, keepdim=True)).cpu().numpy().astype(np.float32)

    def arrays(self, images: list[np.ndarray], pad: bool = False) -> np.ndarray:
        """BGR frames (OpenCV) with the model's own preprocessing. pad: put each image on a black
        square first, as embed.ClipEncoder.arrays does for the crop windows framing.py scores."""
        from PIL import Image

        def rgb(img: np.ndarray) -> Image.Image:
            if pad:
                h, w = img.shape[:2]
                n = max(h, w)
                sq = np.zeros((n, n, 3), np.uint8)
                sq[(n - h) // 2:(n - h) // 2 + h, (n - w) // 2:(n - w) // 2 + w] = img
                img = sq
            return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))

        batch = self.torch.stack([self.preprocess(rgb(i)) for i in images])
        with self.torch.no_grad():
            return self._norm(self.model.encode_image(batch))

    def texts(self, texts: list[str]) -> np.ndarray:
        with self.torch.no_grad():
            return self._norm(self.model.encode_text(self.tokenizer(texts)))


def emb_path(settings, name: str):
    return root(settings) / "emb" / f"{name}.npz"


def embed(settings, ts: dict, name: str, threads: int, batch: int = 16) -> None:
    out = emb_path(settings, name)
    if out.exists():
        return
    enc = Encoder(name, threads)
    sids = sorted(ts["scenes"], key=int)
    n, k = len(sids), len(SPOTS)
    valid = np.zeros((n, k), bool)
    items: list[tuple[int, int, np.ndarray]] = []
    for i, sid in enumerate(sids):
        for j, img in enumerate(load_frames(settings, sid)):
            if name in MIDDLE_ONLY and j != 1:
                continue
            if img is not None:
                items.append((i, j, img))
                valid[i, j] = True
    mat = None
    spent, done = 0.0, 0
    for b in range(0, len(items), batch):
        chunk = items[b:b + batch]
        t0 = time.perf_counter()
        vecs = enc.arrays([c[2] for c in chunk])
        spent += time.perf_counter() - t0
        if mat is None:
            mat = np.zeros((n, k, vecs.shape[1]), np.float16)
        for (i, j, _), v in zip(chunk, vecs):
            mat[i, j] = v
        done += len(chunk)
        if done % (batch * 20) < batch or done == len(items):
            status_line(settings, f"embed {name}", done, len(items),
                        f"{spent / done:.3f} s per image")
    queries = [b["query"] for b in ts["blocks"]]
    t0 = time.perf_counter()
    q = enc.texts(queries) if queries else np.zeros((0, mat.shape[2] if mat is not None else 1), np.float32)
    text_secs = (time.perf_counter() - t0) / max(len(queries), 1)
    out.parent.mkdir(exist_ok=True)
    np.savez_compressed(
        out, ids=np.array([int(s) for s in sids]), img=mat if mat is not None else np.zeros((n, k, 1), np.float16),
        valid=valid, queries=q, block_ids=np.array([b["id"] for b in ts["blocks"]]),
        good=enc.texts(INTERESTING), bad=enc.texts(BORING),
        secs_per_image=spent / max(done, 1), secs_per_text=text_secs, load_seconds=enc.load_seconds,
        threads=threads, peak_rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    log.info("%s: %d images, %.3f s each", name, done, spent / max(done, 1))


def load_emb(settings, name: str) -> dict | None:
    p = emb_path(settings, name)
    if not p.exists():
        return None
    with np.load(p) as z:
        return {k: z[k] for k in z.files}


def _embed_one(config_dir, data_dir, folder: str, name: str, threads: int) -> None:
    """One model per child process: its RAM is measured on its own and freed afterwards."""
    from ..config import load_settings
    from .. import bench
    from . import testset

    bench.FOLDER = folder
    settings = load_settings(config_dir, data_dir=data_dir)
    embed(settings, testset.build(settings), name, threads)


def embed_all(config_dir, data_dir, folder: str, names: list[str], threads: int) -> None:
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    for name in names:
        proc = ctx.Process(target=_embed_one, args=(config_dir, data_dir, folder, name, threads))
        proc.start()
        proc.join()
        if proc.exitcode:
            log.warning("model %s failed (exit %s); the others go on", name, proc.exitcode)
