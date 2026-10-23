"""Object finders for the crop test: text-prompted detectors and a "main object" model.

Each takes BGR frames and the block's words and returns the best box as fractions of the frame
(x0, y0, x1, y1, score), or None when it finds nothing. They are optional: a model whose package
or weights can't be loaded is reported as skipped, the rest of the bench goes on."""
from __future__ import annotations

import logging
import urllib.request

import cv2
import numpy as np

from ..framing import MODELS_DIR

log = logging.getLogger(__name__)

THRESHOLD = 0.1
U2NETP_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"


def _best(boxes, scores, w: int, h: int):
    if boxes is None or len(boxes) == 0:
        return None
    i = int(np.argmax(scores))
    if float(scores[i]) < THRESHOLD:
        return None
    x0, y0, x1, y1 = [float(v) for v in boxes[i]]
    return (x0 / w, y0 / h, x1 / w, y1 / h, float(scores[i]))


class Ultra:
    """YOLOE or YOLO-World (Ultralytics): any words as the class to find."""

    def __init__(self, kind: str):
        import ultralytics

        self.kind = kind
        if kind == "yoloe":
            self.model = ultralytics.YOLOE("yoloe-11s-seg.pt")
        else:
            self.model = ultralytics.YOLOWorld("yolov8s-worldv2.pt")
        self._names: list[str] | None = None

    def _set(self, names: list[str]) -> None:
        if names == self._names:
            return
        if self.kind == "yoloe":
            try:
                self.model.set_classes(names, self.model.get_text_pe(names))
            except (AttributeError, TypeError):
                self.model.set_classes(names)
        else:
            self.model.set_classes(names)
        self._names = names

    def find(self, frames: list[np.ndarray], words: str):
        self._set([words])
        best = None
        for r in self.model.predict(frames, verbose=False, conf=THRESHOLD):
            h, w = r.orig_shape
            b = _best(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy(), w, h)
            if b and (best is None or b[4] > best[4]):
                best = b
        return best


class OmDet:
    """OmDet-Turbo (Apache-2.0, in transformers)."""

    def __init__(self):
        from transformers import AutoProcessor, OmDetTurboForObjectDetection

        name = "omlab/omdet-turbo-swin-tiny-hf"
        self.processor = AutoProcessor.from_pretrained(name)
        self.model = OmDetTurboForObjectDetection.from_pretrained(name).eval()

    def find(self, frames: list[np.ndarray], words: str):
        import torch
        from PIL import Image

        best = None
        for f in frames:
            img = Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
            h, w = f.shape[:2]
            try:
                inputs = self.processor(img, text=[words], return_tensors="pt")
            except TypeError:
                inputs = self.processor(img, classes=[words], return_tensors="pt")
            with torch.no_grad():
                out = self.model(**inputs)
            post = self.processor.post_process_grounded_object_detection
            try:
                res = post(out, text_labels=[[words]], target_sizes=[(h, w)], threshold=THRESHOLD)[0]
            except TypeError:
                res = post(out, classes=[[words]], target_sizes=[(h, w)], score_threshold=THRESHOLD)[0]
            b = _best(res["boxes"].numpy(), res["scores"].numpy(), 1, 1)
            if b:
                b = (b[0] / w, b[1] / h, b[2] / w, b[3] / h, b[4])
                if best is None or b[4] > best[4]:
                    best = b
        return best


class U2NetP:
    """Small "main object" model (Apache-2.0, 4.7 MB): no words, just what stands out as a thing."""

    def __init__(self):
        import onnxruntime as ort

        path = MODELS_DIR / "u2netp.onnx"
        if not path.exists():
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(U2NETP_URL, path)
        self.session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    def saliency(self, frame: np.ndarray) -> np.ndarray:
        img = cv2.cvtColor(cv2.resize(frame, (320, 320)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255
        img = (img - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
        x = img.transpose(2, 0, 1)[None].astype(np.float32)
        m = self.session.run(None, {self.input: x})[0][0, 0]
        m = (m - m.min()) / (m.max() - m.min() + 1e-6)
        return cv2.resize(m, (frame.shape[1], frame.shape[0]))

    def find(self, frames: list[np.ndarray], words: str = ""):
        maps = [self.saliency(f) for f in frames]
        m = np.mean(maps, axis=0)
        ys, xs = np.where(m > 0.5)
        if not len(xs):
            return None
        h, w = m.shape
        return (xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h, float(m.max()))


FINDERS = {"yoloe": lambda: Ultra("yoloe"), "yoloworld": lambda: Ultra("yoloworld"),
           "omdet": OmDet, "u2netp": U2NetP}


def load_finder(name: str):
    try:
        return FINDERS[name]()
    except Exception as e:  # noqa: BLE001 - an optional model that can't load is skipped
        log.warning("%s skipped: %s", name, e)
        return None


def box_to_crop(box, fw: float) -> dict:
    """A found box -> where the 9:16 window goes, or the whole frame when the thing is wider."""
    x0, _, x1, _, _ = box
    if x1 - x0 > fw * 1.05:
        return {"x": 0.5, "whole": True}
    return {"x": float(np.clip((x0 + x1) / 2, fw / 2, 1 - fw / 2)), "whole": False}
