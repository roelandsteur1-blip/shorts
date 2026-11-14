"""Source test per channel: run (archives faked), the page, saving the ticked archives."""
import json
import shutil

import pytest

from shorts import sourcetest
from shorts.config import ROOT, load_channel


@pytest.fixture
def cfg(tmp_path, settings):
    d = tmp_path / "config"
    shutil.copytree(ROOT / "config", d, ignore=shutil.ignore_patterns("local.toml", "*.bak"))
    settings.config_root = d
    return d




def test_defaults_from_the_research(cfg):
    ch = load_channel("space", cfg)
    assert sourcetest.default_subjects(ch)[:2] == ["Saturn", "International Space Station"] or \
        len(sourcetest.default_subjects(ch)) == 6
    assert "NASA video" in sourcetest.picked(ch) and sourcetest.picked(ch)[0] == sourcetest.LIBRARY


def test_run_writes_sheet_and_counts(settings, cfg, monkeypatch, conn):
    from meta.collector import sweep
    ch = load_channel("space", cfg)
    monkeypatch.setattr(sweep, "load_keys", lambda s: {})
    monkeypatch.setattr(sweep, "Scorer", lambda s: None)
    monkeypatch.setattr(sweep, "wikidata_matches", lambda label: [])
    monkeypatch.setattr(sweep, "claude_qids", lambda subs, m, ask: {0: "Q193"})
    monkeypatch.setattr(sweep, "sources_for", lambda keys: [("Openverse photos", None, None), ("Europeana", None, "europeana")])

    def fake_subject(s, q, row, lib, srcs, keys, scorer):
        assert lib[0] is settings
        row["sources"]["Library today"] = {"found": 1 if s["name"] == "Saturn" else 0}
        row["sources"]["Openverse photos"] = {"hits": [{"ok": True, "thumb_ok": True}]}
        return []
    monkeypatch.setattr(sweep, "sweep_subject", fake_subject)
    monkeypatch.setattr(sweep, "niche_sheet", lambda n, block, cols, path: path.write_bytes(b"jpg"))
    res = sourcetest.run(conn, settings, ch, ["Saturn", " ", "Moon"])
    assert res["columns"] == ["Library today", "Openverse photos"]  # Europeana: no key
    assert res["usable"] == {"Library today": 1, "Openverse photos": 2}
    assert (sourcetest.folder(settings, "space") / "sheet.jpg").exists()
    assert sourcetest.load(settings, "space")["subjects"] == ["Saturn", "Moon"]




