"""Footage on another drive, automatic restock and cleanup of used-up videos."""
from pathlib import Path

from conftest import fake_claude
from shorts import library as lib, pipeline
from shorts.config import load_settings


def test_footage_dir_setting(tmp_path):
    cfg = tmp_path / "config"
    (cfg / "channels").mkdir(parents=True)
    (cfg / "settings.toml").write_text('data_dir = "C:/shorts-data"\n')
    assert load_settings(cfg).footage_dir == Path("C:/shorts-data/footage")
    (cfg / "local.toml").write_text('footage_dir = "D:/shorts-footage"\n')
    assert load_settings(cfg).footage_dir == Path("D:/shorts-footage")


def test_move_footage(library, settings, tmp_path):
    settings.footage_root = tmp_path / "other-drive"
    n, _ = lib.move_footage(library, settings)
    assert n == 2
    for (path,) in library.execute("SELECT path FROM assets"):
        assert Path(path).exists() and Path(path).is_relative_to(settings.footage_root)
    assert lib.move_footage(library, settings)[0] == 0


def test_restock_only_when_low(library, settings, channel, monkeypatch):
    import shorts.ingest_nasa as ingest_mod
    calls = []
    monkeypatch.setattr(ingest_mod, "ingest", lambda *a, **k: calls.append(k) or 0)
    settings.restock_below = 1
    assert lib.restock(library, settings, channel) == 0 and calls == []  # 2 unused videos >= 1
    settings.restock_below = 5
    lib.restock(library, settings, channel)
    assert calls == [{"per_term": settings.restock_per_term}]


def test_cleanup_keeps_footage_until_uploaded(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    sids = pipeline.make(library, settings, channel, count=2)
    # Use every remaining scene so the videos count as used up.
    library.execute("UPDATE scenes SET rejected=1 WHERE id NOT IN (SELECT scene_id FROM blocks) "
                    "AND id NOT IN (SELECT hero_scene_id FROM shorts)")
    library.commit()
    assert lib.cleanup_candidates(library) == []  # Shorts still waiting for review

    library.execute(f"UPDATE shorts SET status='scheduled' WHERE id IN ({','.join(map(str, sids))})")
    library.commit()
    paths = [r[0] for r in library.execute("SELECT path FROM assets")]
    assert lib.cleanup(library, dry_run=True)[0] == 2 and all(Path(p).exists() for p in paths)
    n, _ = lib.cleanup(library)
    assert n == 2 and not any(Path(p).exists() for p in paths)
    assert {r[0] for r in library.execute("SELECT status FROM assets")} == {"cleaned"}
    assert lib.unused_hero_videos(library, settings, channel) == 0
