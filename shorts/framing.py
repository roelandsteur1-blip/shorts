"""Where the subject is in a shot, so the 9:16 crop keeps it in frame (and follows it when it moves).

The old crop centre (scenes.crop_x) was the average position of all detail in one middle frame,
pulled 30% toward the middle: a subject on the left of a busy frame still ended up cut in half.
Here a few frames of the part that is actually shown are read, and for each frame a saliency map
is made (spectral residual: what stands out from the rest of the picture), plus what moves against
the background, plus faces, which count most. The crop window is put where it holds the most of
that map, per frame; the windows over time become a slow pan when the subject moves, else a fixed
position. Runs at render time on small frames (a few hundred ms per block), cached in the render
folder, so every scene in the library benefits without a new scenes pass.

Saliency doesn't know what an aircraft or a planet is, so it can lock onto stars, waves or text.
When the block says what it shows ("fighter jet taking off"), CLIP scores a row of 9:16 windows
across the frame against those words (text_guide): the crop is then pulled to where the words
match, and saliency only fine-tunes inside that area. When the whole frame matches the words
clearly better than any window, the subject doesn't fit in 9:16 (a plane side-on, a skyline) and
the render shows the whole frame on a blurred background instead of cutting it."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

SAMPLE_FPS = 3.0        # frames per second read from the shown part of the scene
MIN_SAMPLES = 3
MAP_W = 96              # saliency is worked out at this width
FACE_WEIGHT = 3.0       # a face counts this much more than the strongest other spot
MOTION_WEIGHT = 0.7
STILL_RANGE = 0.03      # subject moves less than this (fraction of the frame width): no pan
UNSURE = 1.15          # the best window holds barely more than an even share: no subject
MAX_PAN = 0.6           # never pan further than this many window widths in one block
GUIDE_WINDOWS = 7       # 9:16 windows across the frame that CLIP compares with the block's words
GUIDE_FRAMES = 3        # frames of the block they are scored on
GUIDE_SPREAD = 0.012    # best minus worst window below this: the words don't single out a spot
GUIDE_PRIOR = 0.12      # how tightly the crop stays around the words' spot (fraction of the frame)
WHOLE_MARGIN = 0.02     # whole frame beats the best window by this much: show it whole
_local = threading.local()  # one face detector per thread: OpenCV's isn't safe to share


YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
_yunet_path: Path | None | bool = None  # None = not looked for yet, False = not available
_yunet_lock = threading.Lock()


def _yunet_model() -> Path | None:
    """YuNet (OpenCV's small face model, 230 KB, MIT): far better than Haar at side-on and small
    faces. Fetched into models/ the first time; without it (offline) Haar is used."""
    global _yunet_path
    with _yunet_lock:
        if _yunet_path is None:
            path = MODELS_DIR / YUNET_URL.rsplit("/", 1)[1]
            if not path.exists() and hasattr(cv2, "FaceDetectorYN"):
                try:
                    import urllib.request
                    MODELS_DIR.mkdir(exist_ok=True)
                    tmp = path.with_suffix(".part")
                    with urllib.request.urlopen(YUNET_URL, timeout=20) as r:
                        tmp.write_bytes(r.read())
                    tmp.replace(path)
                except Exception as e:  # noqa: BLE001 - offline: Haar does the job
                    log.info("YuNet face model not available (%s): using Haar", e)
            _yunet_path = path if path.exists() and hasattr(cv2, "FaceDetectorYN") else False
        return _yunet_path or None


def _faces(frame: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Faces as (x, y, w, h) in a BGR frame: YuNet when it is there, else Haar."""
    h, w = frame.shape[:2]
    model = _yunet_model()
    if model:
        global _yunet_path
        try:
            det = getattr(_local, "yunet", None)
            if det is None:
                det = _local.yunet = cv2.FaceDetectorYN.create(str(model), "", (w, h), 0.7, 0.3, 50)
            det.setInputSize((w, h))
            _, found = det.detect(frame)
            return [tuple(int(v) for v in f[:4]) for f in found] if found is not None else []
        except cv2.error as e:  # a broken model file or an OpenCV without it: Haar from now on
            log.warning("YuNet failed (%s): using Haar", e)
            _yunet_path = False
    cascade = getattr(_local, "cascade", None)
    if cascade is None:
        if not hasattr(cv2, "CascadeClassifier"):  # OpenCV 5 dropped it: no faces, the rest still works
            cascade = False
        else:
            cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        _local.cascade = cascade
    if not cascade or cascade.empty():
        return []
    found = cascade.detectMultiScale(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), scaleFactor=1.1,
                                     minNeighbors=6, minSize=(16, 16))
    return [tuple(int(v) for v in f) for f in found] if len(found) else []


def _norm(a: np.ndarray) -> np.ndarray:
    a = a - a.min()
    m = a.max()
    return a / m if m > 1e-9 else a


def spectral_residual(gray: np.ndarray) -> np.ndarray:
    """Hou & Zhang (2007): the part of the log spectrum that differs from its local average is
    what stands out in the picture. gray: float32 0..1 at MAP_W wide."""
    f = np.fft.fft2(gray)
    log_amp = np.log(np.abs(f) + 1e-8)
    residual = log_amp - cv2.blur(log_amp.astype(np.float32), (3, 3))
    sal = np.abs(np.fft.ifft2(np.exp(residual + 1j * np.angle(f)))) ** 2
    return _norm(cv2.GaussianBlur(sal.astype(np.float32), (0, 0), 2.5))


def saliency_map(frame: np.ndarray, prev: np.ndarray | None, faces: list | None = None) -> np.ndarray:
    """One map per frame (MAP_W wide): stand-out detail, motion against the background, faces."""
    h, w = frame.shape[:2]
    mh = max(2, round(MAP_W * h / w))
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (MAP_W, mh), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    sal = spectral_residual(small)
    if prev is not None:
        p = cv2.resize(cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY), (MAP_W, mh), interpolation=cv2.INTER_AREA)
        motion = np.abs(small - p.astype(np.float32) / 255)
        # A camera pan moves everything: only motion well above the frame's typical change counts.
        motion = np.clip(motion - np.median(motion) * 2, 0, None)
        motion = cv2.GaussianBlur(motion, (0, 0), 2.0)
        if motion.max() > 0.02:
            sal = sal + MOTION_WEIGHT * _norm(motion)
    sal = _norm(sal)
    # Logos and burnt-in captions sit at the top and bottom: worth less than the middle.
    ys = np.linspace(0, 1, mh)[:, None]
    sal = sal * np.where((ys < 0.1) | (ys > 0.72), 0.4, 1.0)
    for (x, y, fw, fh) in (_faces(frame) if faces is None else faces):
        cx, cy, r = (x + fw / 2) * MAP_W / w, (y + fh / 2) * mh / h, max(fw * MAP_W / w, 1.0)
        yy, xx = np.mgrid[0:mh, 0:MAP_W]
        sal = sal + FACE_WEIGHT * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * r * r)))
    return sal


def best_window(profile: np.ndarray, frac: float, prior: float = 0.25, centre: float = 0.5) -> tuple[float, float]:
    """(centre 0..1, share of the profile inside) of the window `frac` wide holding the most of
    `profile`. A wide, mild pull to the middle decides when the picture is evenly busy; with a
    text guide the pull is to where the block's words match, and tighter."""
    n = profile.size
    if frac >= 1 or n < 2:
        return 0.5, 1.0
    xs = (np.arange(n) + 0.5) / n
    weighted = profile * np.exp(-((xs - centre) ** 2) / (2 * prior ** 2))
    k = max(1, round(frac * n))
    sums = np.convolve(weighted, np.ones(k), mode="valid")
    i = int(np.argmax(sums))
    total = float(profile.sum())
    inside = float(profile[i:i + k].sum()) / total if total > 0 else frac
    if inside < frac * UNSURE:  # nothing stands out (dark sky, sea, fog): keep the middle
        return centre, inside
    return (i + k / 2) / n, inside


def window_fraction(width: int, height: int) -> tuple[float, float]:
    """Width and height of the 9:16 window as a fraction of the source frame."""
    aspect = (width or 16) / max(height or 9, 1)
    target = 9 / 16
    return (target / aspect, 1.0) if aspect >= target else (1.0, aspect / target)


def text_guide(frames: list[np.ndarray], fw: float, query: str, encoder) -> dict | None:
    """Where across the frame the block's words match best: CLIP similarity of GUIDE_WINDOWS
    windows (fw wide) and of the whole frame, averaged over a few frames. None when the crop is
    not sideways, there is nothing to go on, or the encoder can't take images in memory."""
    if fw >= 1 or not frames or not query or not hasattr(encoder, "arrays"):
        return None
    picked = [frames[int(i)] for i in np.linspace(0, len(frames) - 1, min(GUIDE_FRAMES, len(frames)))]
    centres = np.linspace(fw / 2, 1 - fw / 2, GUIDE_WINDOWS)
    crops = []
    for fr in picked:
        w = fr.shape[1]
        cw = max(1, round(fw * w))
        for c in centres:
            x = int(np.clip(round((c - fw / 2) * w), 0, w - cw))
            crops.append(fr[:, x:x + cw])
        crops.append(fr)
    vecs = encoder.arrays(crops)
    q = encoder.texts([query])[0]
    sims = (vecs @ q).reshape(len(picked), GUIDE_WINDOWS + 1).mean(axis=0)
    windows, whole = sims[:-1], float(sims[-1])
    best = int(np.argmax(windows))
    return {"centre": float(centres[best]), "spread": float(windows.max() - windows.min()),
            "best": float(windows[best]), "whole": whole}


def analyse(frames: list[np.ndarray], width: int, height: int, guide: dict | None = None) -> dict:
    """{'x0', 'x1', 'y0', 'y1'}: subject centre (fractions of the source frame) at the start
    and end of the block, plus 'fx', 'fy' (where the subject sits for zooming) and 'fit'.
    guide: text_guide()'s answer; adds 'whole' (show the whole frame) when the subject won't fit."""
    fw, fh = window_fraction(width, height)
    aim = guide is not None and guide["spread"] >= GUIDE_SPREAD
    prior, centre_x = (GUIDE_PRIOR, guide["centre"]) if aim else (0.25, 0.5)
    centres, ys, fits, moves = [], [], [], []
    faces = 0
    prev = None
    for fr in frames:
        if prev is not None:
            moves.append(float(np.mean(np.abs(fr.astype(np.float32) - prev.astype(np.float32)))) / 255)
        found = _faces(fr)
        faces += len(found)
        sal = saliency_map(fr, prev, found)
        prev = fr
        cx, fit_x = best_window(sal.sum(axis=0), fw, prior, centre_x)
        cy, fit_y = best_window(sal.sum(axis=1), fh)
        # Vertical spot of the subject inside the chosen columns: used to aim zooms.
        n = sal.shape[1]
        lo, hi = int(max(0, (cx - fw / 2) * n)), int(min(n, (cx + fw / 2) * n + 1))
        rows = sal[:, lo:hi].sum(axis=1)
        sy = float((rows * (np.arange(rows.size) + 0.5)).sum() / rows.sum() / rows.size) if rows.sum() > 0 else 0.5
        centres.append((cx, cy))
        ys.append(sy)
        fits.append(min(fit_x, fit_y))
    whole = guide is not None and guide["whole"] - guide["best"] >= WHOLE_MARGIN and not faces
    if not centres:
        return {"x0": 0.5, "x1": 0.5, "y0": 0.5, "y1": 0.5, "fy": 0.5, "fit": 0.0, "motion": None, "faces": 0,
                "whole": whole}
    c = np.array(centres)
    t = np.linspace(0, 1, len(c))
    path = []
    for axis, frac in ((0, fw), (1, fh)):
        v = c[:, axis]
        if frac >= 1 or v.max() - v.min() < STILL_RANGE or len(v) < 3:
            a = b = float(np.median(v))
        else:
            # Straight line through the centres: one calm pan, no wobble following each frame.
            slope, icpt = np.polyfit(t, v, 1)
            a, b = float(icpt), float(icpt + slope)
            resid = float(np.abs(v - (icpt + slope * t)).max())
            if resid > frac * 0.5:  # the subject jumps about: hold the middle of where it was
                a = b = float(np.median(v))
            elif abs(b - a) > frac * MAX_PAN:
                mid, half = (a + b) / 2, frac * MAX_PAN / 2
                a, b = (mid - half, mid + half) if b > a else (mid + half, mid - half)
            if abs(b - a) < STILL_RANGE:
                a = b = (a + b) / 2
        lo, hi = frac / 2, 1 - frac / 2
        path += [float(np.clip(a, lo, hi)), float(np.clip(b, lo, hi))]
    return {"x0": path[0], "x1": path[1], "y0": path[2], "y1": path[3],
            "fy": float(np.clip(np.median(ys), 0.25, 0.75)), "fit": float(np.mean(fits)),
            "motion": float(np.mean(moves)) if moves else None, "faces": faces, "whole": whole}


def read_frames(path: str, start: float, seconds: float, width: int, height: int) -> list[np.ndarray]:
    from .scenes import FRAME_W, _ffmpeg_frames, _frame_height
    fps = max(SAMPLE_FPS, MIN_SAMPLES / max(seconds, 0.1))
    return list(_ffmpeg_frames(path, start, max(seconds, 0.2), fps, FRAME_W,
                               _frame_height(width, height), "cpu"))


def subject_path(path: str, start: float, seconds: float, width: int, height: int,
                 fallback_x: float = 0.5, cache: dict | None = None, query: str | None = None,
                 encoder=None) -> dict:
    """analyse() for the part of `path` from `start` that a block shows, cached by position.
    query + encoder: the block's subject in words and CLIP, to aim the crop (text_guide).
    Any failure (unreadable file, no frames) falls back to the scene's stored crop centre."""
    key = f"{path}|{start:.2f}|{seconds:.2f}" + (f"|{query}" if query and encoder is not None else "")
    if cache is not None and key in cache:
        return cache[key]
    try:
        frames = read_frames(path, start, seconds, width, height)
        guide = None
        if frames and query and encoder is not None:
            try:
                guide = text_guide(frames, window_fraction(width, height)[0], query, encoder)
            except Exception as e:  # noqa: BLE001 - without the words, saliency still frames it
                log.warning("text-guided framing failed for %s: %s", path, e)
        res = analyse(frames, width, height, guide) if frames else None
    except Exception as e:  # noqa: BLE001 - framing is a nicety, a render must not fail on it
        log.warning("framing failed for %s at %.1fs: %s", path, start, e)
        res = None
    if res is None:
        fw, _ = window_fraction(width, height)
        x = float(np.clip(fallback_x, fw / 2, 1 - fw / 2))
        res = {"x0": x, "x1": x, "y0": 0.5, "y1": 0.5, "fy": 0.5, "fit": 0.0, "motion": None, "faces": 0,
               "whole": False}
    if cache is not None:
        cache[key] = res
    return res


def load_cache(work: Path) -> dict:
    try:
        return json.loads((work / "framing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(work: Path, cache: dict) -> None:
    (work / "framing.json").write_text(json.dumps(cache, indent=1), encoding="utf-8")
