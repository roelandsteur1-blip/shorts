"""Delete what the classic pipeline left on the server (Roeland chose "also footage and renders,
~105 GB" on 2026-11-14, after the classicoff disk report). One fixed list, nothing else:

- shorts-data: footage (all downloaded classic footage, incl. half-finished tmp* downloads),
  renders, keyframes, bench, bench-smoke
- ~/bench-venv (the clip-quality bench's own venv)
- meta-out research output of the old makers: pieces, textcard, quiz, ranking, round3, and the
  samples/textcard tarballs

Kept: the database and its backups, music, secrets, logs, everything transcript-first
(~/meta-out/tclips, transcripts, videos), meta-out/clips (meta/tclips imports meta/clips), the
research venv and the results clone.

    python -m meta.classicpurge          # deletes, prints what and how much
    python -m meta.classicpurge --dry    # only prints

The database rows of the classic Shorts and footage stay; their files are gone, so bringing the
classic pipeline back means downloading footage again (archive/classic-dashboard/README.md).
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from . import config_dir
from .classicoff import gb, size_of

HOME = Path.home()
OUT = HOME / "meta-out" / "classicoff"
DATA_PARTS = ("footage", "renders", "keyframes", "bench", "bench-smoke")
META_PARTS = ("pieces", "textcard", "quiz", "ranking", "round3", "samples.tar.gz", "textcard.tar.gz")
KEEP = ("tclips", "transcripts", "videos", "clips", "runner-done.json", "classicoff")


def targets(settings) -> list[Path]:
    data = Path(settings.data_dir)
    out = [data / p for p in DATA_PARTS]
    footage = Path(settings.footage_dir)
    if footage.resolve() != (data / "footage").resolve():
        out.append(footage)  # footage_dir set elsewhere in local.toml
    out.append(HOME / "bench-venv")
    out += [HOME / "meta-out" / p for p in META_PARTS]
    return out


def safe(path: Path, settings) -> bool:
    """Only a direct child of data_dir / meta-out / home (or the configured footage folder), never a
    root itself, never something transcript-first uses or the database."""
    p = path.resolve()
    data, meta_out = Path(settings.data_dir).resolve(), (HOME / "meta-out").resolve()
    if p in {HOME.resolve(), Path("/"), data, meta_out}:
        return False
    if p == Path(settings.footage_dir).resolve():
        return True
    return p.parent in {data, meta_out, HOME.resolve()} and p.name not in KEEP and not p.name.startswith("shorts.db")


def purge(settings, dry: bool = False) -> list[dict]:
    rows = []
    for t in targets(settings):
        if not t.exists() and not t.is_symlink():
            continue
        if not safe(t, settings):
            print(f"!! skipped (not on the fixed list): {t}")
            continue
        n = size_of(t) if t.is_dir() else t.stat().st_size
        rows.append({"path": str(t), "bytes": n})
        print(f"{'would delete' if dry else 'deleting'} {gb(n):>9}  {t}")
        if dry:
            continue
        if t.is_dir() and not t.is_symlink():
            shutil.rmtree(t, ignore_errors=True)
        else:
            t.unlink(missing_ok=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    from shorts.config import load_settings
    settings = load_settings(config_dir())
    before = shutil.disk_usage(str(HOME)).free
    rows = purge(settings, dry=args.dry)
    after = shutil.disk_usage(str(HOME)).free
    print(f"== {gb(sum(r['bytes'] for r in rows))} in {len(rows)} folders/files; free {gb(before)} -> {gb(after)}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "classicpurge.json").write_text(json.dumps({"dry": args.dry, "deleted": rows, "free_before": before,
                                                       "free_after": after}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
