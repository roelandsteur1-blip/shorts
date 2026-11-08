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


@pytest.fixture
def client(settings, cfg):
    from shorts.web import create_app
    return create_app(settings, start_runner=False).test_client()


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


def test_page_run_and_save(client, settings, cfg, conn):
    page = client.get("/channels/space/source-test")
    assert page.status_code == 200 and b"Run source test" in page.data and b"Saturn" in page.data
    assert client.get("/channels/space/source-test/sheet.jpg").status_code == 404
    r = client.post("/channels/space/source-test/run", data={"subjects": "Saturn\nMoon | x\n"})
    assert r.status_code == 302
    row = conn.execute("SELECT command FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(row[0]) == ["source-test", "--subjects", "Saturn|Moon   x"]
    out = sourcetest.folder(settings, "space")
    out.mkdir(parents=True)
    (out / "sheet.jpg").write_bytes(b"jpg")
    (out / "result.json").write_text(json.dumps({"columns": ["Library today", "Openverse photos", "NASA video"],
                                                 "usable": {"Library today": 3}, "subjects": ["Saturn"],
                                                 "at": "2026-11-05T20:00", "minutes": 2}))
    page = client.get("/channels/space/source-test")
    assert b"3 of 1 subjects" in page.data or b"(3 of 1" in page.data
    assert client.get("/channels/space/source-test/sheet.jpg").data == b"jpg"
    assert client.post("/channels/space/source-test/save", data={"source": ["bogus"]}).status_code == 400
    r = client.post("/channels/space/source-test/save", data={"source": ["Library today", "NASA video", "bogus"]})
    assert r.status_code == 302
    assert load_channel("space", cfg).collector_sources == ["Library today", "NASA video"]
    assert b"Your saved choice" in client.get("/channels/space/source-test").data
    assert b"Open source test" in client.get("/channels/space").data


def test_tick_without_a_test_and_copy_sibling_pick(client, settings, cfg):
    """Every archive the collector can search is tickable before any source test, and a channel
    without its own pick copies the saved pick of a channel with the same niche."""
    from shorts.collector import COLUMNS
    ch = load_channel("space", cfg)
    assert sourcetest.choices(ch) == [sourcetest.LIBRARY] + list(COLUMNS)
    from meta.collector import sweep
    every = [n for n, _, k in sweep.sources_for({})]
    page = client.get("/channels/space/source-test").data
    for col in COLUMNS:
        assert col == "Pixabay (B-roll)" or col.encode() in page  # Pixabay: no key in tests
    for col in ("Internet Archive", "Prelinger", "NASA SVS"):  # same list as after a test
        assert col in every and col.encode() in page
    r = client.post("/channels/space/source-test/save", data={"source": ["Commons video (name)", "NASA video"]})
    assert r.status_code == 302
    assert load_channel("space", cfg).collector_sources == ["Commons video (name)", "NASA video"]
    # a second space channel without ticks borrows the first one's pick
    text = (cfg / "channels" / "space.toml").read_text(encoding="utf-8")
    text = "\n".join(x for x in text.splitlines() if not x.startswith("collector_sources"))
    (cfg / "channels" / "spacetwo.toml").write_text(text.replace('id = "space"', 'id = "spacetwo"', 1), encoding="utf-8")
    two = load_channel("spacetwo", cfg)
    assert not two.collector_sources
    assert sourcetest.picked(two, cfg) == ["Commons video (name)", "NASA video"]
    assert sourcetest.picked(two) != ["Commons video (name)", "NASA video"]  # no config dir: research default
    assert b"saved choice of" in client.get("/channels/spacetwo/source-test").data
