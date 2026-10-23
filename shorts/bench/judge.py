"""Claude as the judge: labels every pooled candidate clip of every block.

One call per block, with the candidates on a few contact sheets (each clip as a strip of its 3
frames, numbered in random order so position doesn't matter). Labels are kept per block in
judge.json; a stopped run or a Claude usage limit picks up at the next unlabelled block."""
from __future__ import annotations

import logging
import random
import time

import cv2
import numpy as np

from .. import llm
from . import SEED, load, root, save, status_line
from .frames import load_frames

log = logging.getLogger(__name__)

FW, FH = 192, 108   # one frame in a strip
PER_SHEET = 16      # clips per image (2 per row)

TOPIC_PROMPT = """These are numbered video clips. Each clip is shown as 3 frames (start, middle, end of the shot). \
They are candidates for one moment of a YouTube Short.

The Short is about: {topic}
At this moment the on-screen text says: "{text}"
The writer asked for footage of: "{visual}"

Rate every clip:
- topic: 2 = clearly shows what the text talks about (the thing itself is visible and recognisable); \
1 = related (same theme, place, era or kind of thing) but not what the text is about; \
0 = unrelated, or unusable (black, title card, test pattern, mostly text).
- interest: 1-5, how much an ordinary viewer scrolling Shorts would want to keep watching this clip, \
judged on its own and not on the topic: 5 = striking, one clear subject, motion or drama; \
3 = fine but ordinary; 1 = dull, empty, cluttered or ugly.

Answer with only JSON, every clip number once: {{"1": [topic, interest], "2": [topic, interest], ...}}"""


def strip(frames: list[np.ndarray | None], label: str) -> np.ndarray:
    out = np.zeros((FH, FW * len(frames), 3), np.uint8)
    for i, f in enumerate(frames):
        if f is None:
            continue
        h, w = f.shape[:2]
        s = min(FW / w, FH / h)
        nw, nh = max(1, round(w * s)), max(1, round(h * s))
        x, y = i * FW + (FW - nw) // 2, (FH - nh) // 2
        out[y:y + nh, x:x + nw] = cv2.resize(f, (nw, nh), interpolation=cv2.INTER_AREA)
    label_box(out, label)
    return out


def label_box(img: np.ndarray, label: str, scale: float = 0.7) -> None:
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    cv2.rectangle(img, (0, 0), (tw + 10, th + 12), (0, 0, 0), -1)
    cv2.putText(img, label, (5, th + 5), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 0), 2, cv2.LINE_AA)


def grid(tiles: list[np.ndarray], cols: int, gap: int = 6) -> np.ndarray:
    h = max(t.shape[0] for t in tiles)
    w = max(t.shape[1] for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    out = np.full((rows * (h + gap) - gap, cols * (w + gap) - gap, 3), 70, np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        out[r * (h + gap):r * (h + gap) + t.shape[0], c * (w + gap):c * (w + gap) + t.shape[1]] = t
    return out


def jpeg(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("could not encode sheet")
    return buf.tobytes()


def topic_sheets(settings, sids: list[int]) -> list[bytes]:
    tiles = [strip(load_frames(settings, sid), str(i + 1)) for i, sid in enumerate(sids)]
    return [jpeg(grid(tiles[i:i + PER_SHEET], 2)) for i in range(0, len(tiles), PER_SHEET)]


def pace(seconds: float, last: list[float]) -> None:
    """Wait so Claude calls are at least `seconds` apart: the bench must leave the Pro limit
    room for the daily run's scripts."""
    if last and seconds > 0:
        wait = last[0] + seconds - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    last[:] = [time.monotonic()]


LIMIT_WAIT = 30 * 60          # a usage limit resets within 5 hours: try again every half hour
LIMIT_GIVE_UP = 24 * 3600     # ...but stop after a day (a weekly limit)


def ask(settings, prompt: str, images: list[bytes], waited: list[float]):
    """llm.ask_json, but a Claude usage limit is waited out: the run is unattended for a day or
    more, and stopping would leave it idle until someone starts it again."""
    while True:
        try:
            return llm.ask_json(prompt, settings.claude_cmd, stage="bench-judge", images=images)
        except llm.LLMLimitError:
            if waited[0] >= LIMIT_GIVE_UP:
                raise
            log.warning("Claude usage limit: trying again in %d minutes", LIMIT_WAIT // 60)
            status_line(settings, "judge waiting", 0, 0, "Claude usage limit reached, trying again every 30 min")
            time.sleep(LIMIT_WAIT)
            waited[0] += LIMIT_WAIT
            llm.reset_limit()


def run_topic(settings, ts: dict, pools: dict[str, list[int]], limit: int | None = None,
              every: float = 0.0) -> dict:
    path = root(settings) / "judge.json"
    labels = load(path, {})
    todo = [b for b in ts["blocks"] if str(b["id"]) in pools and
            not set(map(str, pools[str(b["id"])])) <= set(labels.get(str(b["id"]), {}))]
    if limit is not None:
        todo = todo[:limit]
    rng = random.Random(SEED)
    last: list[float] = []
    waited = [0.0]
    for n, b in enumerate(todo):
        key = str(b["id"])
        have = labels.get(key, {})
        sids = [s for s in pools[key] if str(s) not in have]
        rng.shuffle(sids)
        prompt = TOPIC_PROMPT.format(topic=b["topic"] or "?", text=b["text"], visual=b["visual"])
        status_line(settings, "judge topic", n, len(todo), f"block {key}")
        pace(every, last)
        try:
            ans = ask(settings, prompt, topic_sheets(settings, sids), waited)
        except llm.LLMLimitError:
            log.warning("Claude usage limit for a day: judging stops here; run again after it resets")
            break
        except llm.LLMError as e:
            log.warning("judge failed for block %s: %s", key, e)
            continue
        got = dict(have)
        for i, sid in enumerate(sids):
            v = ans.get(str(i + 1)) if isinstance(ans, dict) else None
            if isinstance(v, list) and len(v) == 2:
                got[str(sid)] = [int(v[0]), int(v[1])]
        labels[key] = got
        save(path, labels)
    return labels
