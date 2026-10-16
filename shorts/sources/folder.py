"""Sources without an API (Mixkit, Dareful, Vidsplay): download the files yourself into
<data_dir>/import/<source>/, then import them. The license is asked once per batch on the
dashboard (or given with --license / --credit); imported originals move to an 'imported' subfolder."""
from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from .. import dedupe, media, progress
from ..config import Settings
from . import Item, Rights

log = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpg", ".mpeg", ".ogv"}

FOLDERS = {
    "mixkit": dict(LABEL="Mixkit", LICENSE="Mixkit Free License (videos; don't use Mixkit music)",
                   default_license="Mixkit Free License", default_credit="",
                   NOTE="Video license is fine for YouTube. Don't use Mixkit music."),
    "dareful": dict(LABEL="Dareful", LICENSE="CC BY 4.0: credit 'Joel Holland / Dareful' needed",
                    default_license="CC BY 4.0", default_credit="Joel Holland / Dareful (CC BY 4.0)",
                    NOTE="Small 4K nature and city set."),
    "vidsplay": dict(LABEL="Vidsplay", LICENSE="Vidsplay License: visible credit in the description",
                     default_license="Vidsplay License", default_credit="Vidsplay.com",
                     NOTE="Must be part of a larger edit, which a Short is."),
}


def source(name: str) -> SimpleNamespace:
    """A module-like object with the same fields the API sources have."""
    cfg = FOLDERS[name]
    return SimpleNamespace(
        NAME=name, NEEDS_KEY=False, HERO_OK=False, IMPORT_ONLY=True,
        folder_hint=f"<data_dir>\\import\\{name}", **cfg,
        search=lambda *a, **k: [], rights=lambda item: Rights(license=cfg["default_license"]),
        file_url=lambda *a, **k: None,
    )


def folder(settings: Settings, name: str) -> Path:
    return settings.data_dir / "import" / name


def waiting(settings: Settings, name: str) -> list[Path]:
    d = folder(settings, name)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT)


def _file_id(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
    return f"{h.hexdigest()[:16]}-{path.stat().st_size}"


def import_folder(conn: sqlite3.Connection, settings: Settings, name: str,
                  license: str | None = None, credit: str | None = None) -> int:
    """Ingest every video waiting in the source's import folder. Returns the number added."""
    from .common import MIN_SECONDS, MAX_SECONDS, _slug, exists, upsert_item

    cfg = FOLDERS[name]
    license = (license or cfg["default_license"]).strip()
    credit = (credit if credit is not None else cfg["default_credit"]).strip() or None
    files = waiting(settings, name)
    done_dir = folder(settings, name) / "imported"
    added = 0
    for n, path in enumerate(files, 1):
        progress.report(n - 1, len(files), f"import {name}")
        fid = _file_id(path)
        if exists(conn, name, fid):
            log.info("%s: already imported", path.name)
        else:
            item = Item(source=name, source_id=fid, title=path.stem.replace("_", " ").replace("-", " "),
                        source_url=cfg["LABEL"])
            asset_id = upsert_item(conn, item, Rights(license=license, attribution=credit))
            try:
                info = media.probe(path)
                if not MIN_SECONDS <= info["duration"] <= MAX_SECONDS:
                    raise ValueError(f"duration {info['duration']:.0f}s out of range")
                fp = dedupe.fingerprint(str(path))
                if fp and (orig := dedupe.same_footage(conn, fp, asset_id)) is not None:
                    dedupe.mark_duplicate(conn, asset_id, orig, "same footage")
                else:
                    dst = settings.footage_dir / name / f"{_slug(fid)}.mp4"
                    media.strip_audio(path, dst)
                    conn.execute("""UPDATE assets SET path=?, duration=?, width=?, height=?, fingerprint=?,
                                    status='downloaded' WHERE id=?""",
                                 (str(dst), info["duration"], info["width"], info["height"], fp, asset_id))
                    conn.commit()
                    added += 1
            except Exception as e:  # noqa: BLE001 - record and continue with the next file
                log.warning("%s failed: %s", path.name, e)
                conn.execute("UPDATE assets SET status='failed', error=? WHERE id=?", (str(e)[:500], asset_id))
                conn.commit()
        done_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(done_dir / path.name))
    progress.report(len(files), len(files), f"import {name}")
    return added
