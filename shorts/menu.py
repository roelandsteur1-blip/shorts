"""What the script writer sees of the footage: one contact sheet per hero shot.

The top row is three frames of the hero shot (so the hook is about what is really on screen); below
it a numbered menu of clips (C1, C2, ...) the blocks can be put on: first scenes near the hero in
its own video (same subject, same look), then the library scenes that look most like the hero and
match its video's title. Claude picks a clip per block from what it has seen, instead of writing a
wish that CLIP then matches to whatever is left (shorts/shots.py does that only when Claude says
no clip fits)."""
from __future__ import annotations

import logging
import sqlite3

import cv2
import numpy as np

from . import db, embed, fit
from .config import Channel, Settings

log = logging.getLogger(__name__)

MENU_SIZE = 16        # clips per hero (4 rows of 4 under the hero row)
OWN_MAX = 6           # at most this many from the hero's own video
PER_VIDEO = 2         # at most this many from any other video
MIN_CLIP_SECONDS = 1.5
TILE_W, TILE_H, COLS = 256, 144, 4
LABEL_H = 4           # gap between rows


def own_scenes(conn: sqlite3.Connection, settings: Settings, hero: sqlite3.Row, exclude: set[int]) -> list[sqlite3.Row]:
    """Unused scenes of the hero's own video that belong to this Short: near the hero (a long video
    gives several Shorts; the rest of it is theirs), or anywhere when the video gives only one."""
    from .hero import budget
    duration = conn.execute("SELECT duration FROM assets WHERE id=?", (hero["asset_id"],)).fetchone()[0]
    near = settings.segment_seconds / 2 if budget(settings, duration) > 1 else float("inf")
    rows = conn.execute(
        """SELECT sc.id, sc.asset_id, sc.start, sc.end, sc.keyframe FROM scenes sc
            WHERE sc.asset_id = ? AND sc.rejected = 0 AND sc.id != ? AND sc.keyframe IS NOT NULL
              AND (sc.end - sc.start) >= ?""",
        (hero["asset_id"], hero["id"], MIN_CLIP_SECONDS)).fetchall()
    rows = [r for r in rows if r["id"] not in exclude and abs(r["start"] - hero["start"]) <= near]
    return sorted(rows, key=lambda r: abs(r["start"] - hero["start"]))[:OWN_MAX]


def library_scenes(conn: sqlite3.Connection, settings: Settings, channel: Channel | None, hero: sqlite3.Row,
                   exclude: set[int], want: int) -> list[sqlite3.Row]:
    """Scenes from other videos that look like the hero shot and match its video's title/keywords."""
    if want <= 0:
        return []
    a = conn.execute("SELECT title, keywords FROM assets WHERE id=?", (hero["asset_id"],)).fetchone()
    text = f"{a['title'] or ''}. {(a['keywords'] or '')[:150]}".strip(". ")
    space = embed.search_space(conn, settings)
    q = embed.query_encoder(settings, space).texts([text or "archive footage"])[0]
    img = embed.scene_vector(conn, space, hero["id"])
    if img is not None and img.shape == q.shape:
        q = q + img
        q = q / (np.linalg.norm(q) or 1.0)
    allowed = fit.allowed_scenes(conn, channel) if channel else None
    hits = embed.search(conn, settings, text, k=want * 6, exclude=exclude, allowed=allowed, qvec=q, space=space)
    if not hits:
        return []
    ids = [sid for sid, _ in hits]
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT sc.id, sc.asset_id, sc.start, sc.end, sc.keyframe, a.title FROM scenes sc
              JOIN assets a ON a.id = sc.asset_id
             WHERE sc.id IN ({",".join("?" * len(ids))}) AND a.needs_check = 0 AND a.claimed IS NULL
               AND sc.keyframe IS NOT NULL AND (sc.end - sc.start) >= ?""", (*ids, MIN_CLIP_SECONDS))}
    out, per_video = [], {}
    for sid in ids:
        r = rows.get(sid)
        if r is None or r["asset_id"] == hero["asset_id"] or per_video.get(r["asset_id"], 0) >= PER_VIDEO:
            continue
        per_video[r["asset_id"]] = per_video.get(r["asset_id"], 0) + 1
        out.append(r)
        if len(out) >= want:
            break
    return out


def build(conn: sqlite3.Connection, settings: Settings, channel: Channel | None, hero_scene_id: int,
          taken: set[int]) -> dict:
    """{'clips': {'C1': scene_id, ...}, 'lines': [prompt line per clip], 'image': JPEG bytes or None}.
    taken: scenes already on another hero's menu in this batch (added to by this call)."""
    hero = conn.execute("SELECT * FROM scenes WHERE id=?", (hero_scene_id,)).fetchone()
    exclude = db.used_scene_ids(conn, None) | taken | {hero_scene_id}
    own = own_scenes(conn, settings, hero, exclude)
    try:
        other = library_scenes(conn, settings, channel, hero, exclude | {r["id"] for r in own},
                               MENU_SIZE - len(own))
    except Exception as e:  # noqa: BLE001 - the menu is a help: without it the blocks are searched as before
        log.warning("clip menu search failed: %s", e)
        other = []
    clips, lines, tiles = {}, [], []
    for i, r in enumerate(own + other, 1):
        label = f"C{i}"
        clips[label] = r["id"]
        taken.add(r["id"])
        where = "same video" if r["asset_id"] == hero["asset_id"] else f'from "{(r["title"] or "")[:60]}"'
        lines.append(f"{label} {r['end'] - r['start']:.0f}s {where}")
        tiles.append((label, _read_image(r["keyframe"])))
    try:
        image = sheet(hero_frames(conn, hero), tiles)
    except Exception as e:  # noqa: BLE001
        log.warning("contact sheet failed for scene %s: %s", hero_scene_id, e)
        image = None
    return {"clips": clips if image else {}, "lines": lines if image else [], "image": image}


def hero_frames(conn: sqlite3.Connection, hero: sqlite3.Row, n: int = 3) -> list[np.ndarray | None]:
    path = conn.execute("SELECT path FROM assets WHERE id=?", (hero["asset_id"],)).fetchone()[0]
    span = hero["end"] - hero["start"]
    times = [hero["start"] + span * f for f in np.linspace(0.15, 0.85, n)]
    cap = cv2.VideoCapture(str(path)) if path else None
    out = []
    try:
        for t in times:
            frame = None
            if cap is not None and cap.isOpened():
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, frame = cap.read()
                frame = frame if ok else None
            out.append(frame)
    finally:
        if cap is not None:
            cap.release()
    if not any(f is not None for f in out) and hero["keyframe"]:
        out = [_read_image(hero["keyframe"])]
    return out


def _read_image(path: str | None) -> np.ndarray | None:
    if not path:
        return None
    img = cv2.imread(str(path))
    return img if img is not None and img.size else None


def _tile(img: np.ndarray | None, label: str) -> np.ndarray:
    """The image letterboxed into one tile, with its label in the top-left corner."""
    t = np.zeros((TILE_H, TILE_W, 3), np.uint8)
    if img is not None:
        h, w = img.shape[:2]
        s = min(TILE_W / w, TILE_H / h)
        nw, nh = max(1, round(w * s)), max(1, round(h * s))
        x, y = (TILE_W - nw) // 2, (TILE_H - nh) // 2
        t[y:y + nh, x:x + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
    cv2.rectangle(t, (0, 0), (tw + 8, th + 10), (0, 0, 0), -1)
    cv2.putText(t, label, (4, th + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
    return t


def sheet(hero: list[np.ndarray | None], clips: list[tuple[str, np.ndarray | None]]) -> bytes:
    """One JPEG: the hero frames on the top row, the clip menu below, COLS tiles per row."""
    rows = [[_tile(f, "HERO") for f in hero[:COLS]]]
    for i in range(0, len(clips), COLS):
        rows.append([_tile(img, label) for label, img in clips[i:i + COLS]])
    gap = np.full((LABEL_H, TILE_W * COLS, 3), 60, np.uint8)
    lines = []
    for r in rows:
        line = np.zeros((TILE_H, TILE_W * COLS, 3), np.uint8)
        for j, t in enumerate(r):
            line[:, j * TILE_W:(j + 1) * TILE_W] = t
        lines += [line, gap]
    ok, buf = cv2.imencode(".jpg", np.vstack(lines[:-1]), [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        raise ValueError("could not encode the contact sheet")
    return buf.tobytes()
