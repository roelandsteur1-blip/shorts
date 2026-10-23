"""Visually interesting: which cheap score agrees best with the judge's interest ratings.

Every labelled candidate scene gets each score; the report compares them with the judge's
interest (1-5, averaged when a scene was rated for several blocks)."""
from __future__ import annotations

import logging
import urllib.request

import cv2
import numpy as np

from . import load, root, save, status_line
from .frames import load_frames
from .models import MODELS, load_emb

log = logging.getLogger(__name__)

AESTHETIC_URL = ("https://github.com/christophschuhmann/improved-aesthetic-predictor/raw/main/"
                 "sac+logos+ava1-l14-linearMSE.pth")


def judged_interest(labels: dict) -> dict[str, float]:
    got: dict[str, list[int]] = {}
    for per_block in labels.values():
        for sid, (_, interest) in per_block.items():
            got.setdefault(sid, []).append(interest)
    return {sid: float(np.mean(v)) for sid, v in got.items()}


def colourfulness(img: np.ndarray) -> float:
    """Hasler & Suesstrunk (2003)."""
    b, g, r = [c.astype(np.float32) for c in cv2.split(img)]
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))


def basics(frames: list[np.ndarray | None]) -> dict:
    fr = [f for f in frames if f is not None]
    if not fr:
        return {}
    mid = fr[len(fr) // 2]
    g = cv2.cvtColor(mid, cv2.COLOR_BGR2GRAY)
    grays = [cv2.cvtColor(cv2.resize(f, (160, 90)), cv2.COLOR_BGR2GRAY).astype(np.float32) for f in fr]
    change = float(np.mean([np.abs(a - b).mean() for a, b in zip(grays[1:], grays[:-1])])) if len(grays) > 1 else 0.0
    return {"sharpness": float(cv2.Laplacian(g, cv2.CV_64F).var()),
            "colour": colourfulness(mid),
            "busy": float((cv2.Canny(g, 100, 200) > 0).mean()),
            "change": change}


def _aesthetic(settings, sids: list[str], threads: int) -> dict[str, float]:
    """LAION's aesthetic predictor (a small head on CLIP ViT-L/14 OpenAI image embeddings)."""
    import open_clip
    import torch
    from PIL import Image

    from ..framing import MODELS_DIR

    torch.set_num_threads(threads)
    wpath = MODELS_DIR / "laion-aesthetic-l14.pth"
    if not wpath.exists():
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(AESTHETIC_URL, wpath)
    head = torch.nn.Sequential(torch.nn.Linear(768, 1024), torch.nn.Dropout(0.2), torch.nn.Linear(1024, 128),
                               torch.nn.Dropout(0.2), torch.nn.Linear(128, 64), torch.nn.Dropout(0.1),
                               torch.nn.Linear(64, 16), torch.nn.Linear(16, 1))
    state = torch.load(wpath, map_location="cpu")
    head.load_state_dict({k.replace("layers.", ""): v for k, v in state.items()})
    head.eval()
    model, _, prep = open_clip.create_model_and_transforms("ViT-L-14", pretrained="openai")
    model.eval()
    out = {}
    for i in range(0, len(sids), 16):
        chunk = sids[i:i + 16]
        imgs, keep = [], []
        for sid in chunk:
            f = load_frames(settings, sid)[1]
            if f is not None:
                imgs.append(prep(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))))
                keep.append(sid)
        if not imgs:
            continue
        with torch.no_grad():
            e = model.encode_image(torch.stack(imgs)).float()
            e = e / e.norm(dim=-1, keepdim=True)
            scores = head(e).squeeze(1).tolist()
        out.update(zip(keep, scores))
        status_line(settings, "interest: aesthetic", i + len(chunk), len(sids))
    return out


def run(settings, ts: dict, labels: dict, threads: int, aesthetic: bool = True) -> dict:
    path = root(settings) / "interest.json"
    data = load(path, {})
    judged = judged_interest(labels)
    sids = sorted(judged, key=int)
    scores: dict[str, dict[str, float]] = data.get("scores", {})
    for n, sid in enumerate(sids):
        if sid in scores.get("change", {}):
            continue
        for k, v in basics(load_frames(settings, sid)).items():
            scores.setdefault(k, {})[sid] = v
        s = ts["scenes"].get(sid, {})
        m, c = s.get("motion") or 0, s.get("contrast") or 0
        scores.setdefault("today (hero formula)", {})[sid] = (0.45 * min(m / 0.05, 1) + 0.30 * min(c / 0.25, 1))
        if n % 200 == 0:
            status_line(settings, "interest: basics", n, len(sids))
    for name in MODELS:
        e = load_emb(settings, name)
        if e is None:
            continue
        index = {str(i): j for j, i in enumerate(e["ids"])}
        img = e["img"][:, 1].astype(np.float32)
        good, bad = e["good"].mean(axis=0), e["bad"].mean(axis=0)
        key = f"prompts:{name}"
        scores[key] = {sid: float(img[index[sid]] @ good - img[index[sid]] @ bad) for sid in sids if sid in index}
    if aesthetic and "aesthetic (LAION)" not in scores:
        try:
            scores["aesthetic (LAION)"] = _aesthetic(settings, sids, threads)
        except Exception as e:  # noqa: BLE001 - optional
            log.warning("aesthetic predictor skipped: %s", e)
    data = {"judged": judged, "scores": scores}
    save(path, data)
    return data
