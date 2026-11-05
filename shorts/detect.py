"""Subject crop for classic Shorts (research round 2b, meta/collector/crops.py): an open-vocabulary
detector (OWLv2) finds the block's subject on 2 frames; the 9:16 window pans through its box,
or the whole frame is shown on a blurred background when the subject is wider than the window.
When no box is found, the saliency crop (shorts/framing.py) stays. In round 2b this cut the
subject off in 2-6% of clips against 23% for the saliency crop alone."""
from __future__ import annotations

import logging
import threading

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

MODEL = "google/owlv2-base-patch16-ensemble"
MIN_SCORE = 0.15   # boxes below this are noise (crops.py)
FRAMES = 2         # frames per block: 1-2 keep it at roughly 10-25 s on the server CPU
WIDER = 1.1        # subject wider than the window by this factor: show the whole frame

_lock = threading.Lock()
_model = None


class Owl:
    def __init__(self):
        import torch
        from transformers import Owlv2ForObjectDetection, Owlv2Processor
        self.torch = torch
        self.proc = Owlv2Processor.from_pretrained(MODEL)
        self.model = Owlv2ForObjectDetection.from_pretrained(MODEL).eval()

    def box(self, im: Image.Image, text: str) -> tuple[list[float], float] | None:
        """Best box (x0, y0, x1, y1 as fractions of the frame) and its score, or None."""
        W, H = im.size
        with self.torch.no_grad():
            inp = self.proc(text=[[f"a photo of {text}"]], images=im, return_tensors="pt")
            out = self.model(**inp)
            side = max(W, H)  # OWLv2 pads to a square: boxes are relative to the padded side
            # the name moved between transformers versions: try each spelling
            post = (getattr(self.proc, "post_process_object_detection", None)
                    or getattr(self.proc, "post_process_grounded_object_detection", None)
                    or self.proc.image_processor.post_process_object_detection)
            res = post(out, threshold=0.0, target_sizes=self.torch.tensor([[side, side]]))[0]
        scores = res["scores"]
        if len(scores) == 0:
            return None
        i = int(scores.argmax())
        x0, y0, x1, y1 = [float(v) for v in res["boxes"][i]]
        return [max(0, x0 / W), max(0, y0 / H), min(1, x1 / W), min(1, y1 / H)], round(float(scores[i]), 3)


def detector():
    """The loaded model, or None when it can't load (no transformers/torch, no download)."""
    global _model
    with _lock:
        if _model is None:
            try:
                _model = Owl()
            except Exception as e:  # noqa: BLE001 - the saliency crop stays
                log.warning("subject detector not available: %s", str(e)[:200])
                _model = False
        return _model or None


def path_from_boxes(boxes: list[tuple[float, tuple[list[float], float] | None]], fw: float) -> dict | None:
    """Window path from (time, box) pairs: a straight pan through the box centres; whole=True when
    the subject is wider than the window; None when nothing was found."""
    found = [(t, b) for t, b in boxes if b and b[1] >= MIN_SCORE]
    if not found:
        return None
    xs = np.array([(b[0][0] + b[0][2]) / 2 for _, b in found])
    widths = np.array([b[0][2] - b[0][0] for _, b in found])
    lo, hi = fw / 2, 1 - fw / 2
    if len(xs) >= 2 and found[-1][0] > found[0][0]:
        x0, x1 = float(xs[0]), float(xs[-1])
    else:
        x0 = x1 = float(xs.mean())
    return {"x0": float(np.clip(x0, lo, hi)), "x1": float(np.clip(x1, lo, hi)),
            "whole": bool(float(np.median(widths)) > fw * WIDER), "box_score": float(np.median([b[1] for _, b in found]))}


def subject_window(path: str, start: float, seconds: float, width: int, height: int, text: str,
                   cache: dict | None = None) -> dict | None:
    """{x0, x1, whole} for the block, or None (no detector, no box, unreadable file)."""
    from . import framing
    key = f"det|{path}|{start:.2f}|{seconds:.2f}|{text}"
    if cache is not None and key in cache:
        return cache[key]
    res = None
    model = detector() if text else None
    if model:
        try:
            from meta.collector.pieces import frame_at
            times = [start + seconds * (i + 0.5) / FRAMES for i in range(FRAMES)]
            boxes = []
            for t in times:
                im = frame_at(path, t, 640)
                if im is not None:
                    with _lock:  # one detection at a time: the model isn't shared between threads
                        boxes.append((t, model.box(im, text)))
            res = path_from_boxes(boxes, framing.window_fraction(width, height)[0])
        except Exception as e:  # noqa: BLE001 - the saliency crop stays
            log.warning("subject detection failed for %s: %s", path, str(e)[:200])
    if cache is not None:
        cache[key] = res
    return res
