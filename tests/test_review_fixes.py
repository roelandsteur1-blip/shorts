"""Fixes from the code review of 16 Oct 2026."""
import json

import numpy as np
import pytest

from conftest import fake_claude
from shorts import dedupe, embed, factcheck, hero, llm, pipeline, render, script


def test_source_name():
    assert render.source_name("https://www.jpl.nasa.gov/news/x") == "NASA JPL"
    assert render.source_name("https://images.nasa.gov/details/abc") == "NASA Image and Video Library"
    assert render.source_name("https://www.nasa.gov/missions/") == "NASA"
    assert render.source_name("https://www.esa.int/Science") == "ESA"
    assert render.source_name("https://example.org/page") == "example.org"


def test_metadata_strips_angle_brackets_and_fills_empty_title(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    sid = pipeline.make(library, settings, channel, count=1)[0]
    library.execute("UPDATE shorts SET title='', description='Mass <1%> of Earth' WHERE id=?", (sid,))
    meta = render.metadata(library, channel, sid)
    assert meta["title"] == "topic 0"
    assert "<" not in meta["description"] and ">" not in meta["description"]


def test_scripts_only_attach_to_valid_hero_numbers(library, settings, channel, monkeypatch):
    heroes = hero.candidates(library, settings, channel.id, 2)
    reply = [{"topic": "no number", "blocks": [{"text": "a b c"}] * 4},
             {"hero": 0, "topic": "zero", "blocks": [{"text": "a b c"}] * 4},
             {"hero": 2, "topic": "two", "blocks": [{"text": "a b c"}] * 4},
             {"hero": 2, "topic": "two again", "blocks": [{"text": "a b c"}] * 4}]
    monkeypatch.setattr(llm, "ask", lambda *a, **k: json.dumps(reply))
    created = script.write_scripts(library, settings, channel, "what_youre_seeing", heroes)
    assert len(created) == 1
    row = library.execute("SELECT topic, hero_scene_id FROM shorts WHERE id=?", (created[0],)).fetchone()
    assert (row["topic"], row["hero_scene_id"]) == ("two", heroes[1])


def test_factcheck_ignores_unknown_short_ids(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    heroes = hero.candidates(library, settings, channel.id, 1)
    sid = script.write_scripts(library, settings, channel, "what_youre_seeing", heroes)[0]
    monkeypatch.setattr(llm, "ask", lambda *a, **k: json.dumps(
        [{"short": None, "facts": []}, {"short": 999, "facts": []}]))
    factcheck.check(library, settings, [sid])
    assert library.execute("SELECT status FROM shorts WHERE id=?", (sid,)).fetchone()[0] == "scripted"


def test_llm_timeout_is_an_llm_error(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("claude", 1)
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(llm.LLMError):
        llm.ask("hi", timeout=1)


def test_hero_skips_scenes_used_as_broll(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    sid = pipeline.make(library, settings, channel, count=1)[0]
    used = {r[0] for r in library.execute("SELECT scene_id FROM blocks WHERE short_id=?", (sid,))}
    assert not used & set(hero.candidates(library, settings, channel.id, 50))


def test_rerender_keeps_approval(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    sid = pipeline.make(library, settings, channel, count=1)[0]
    library.execute("UPDATE shorts SET status='approved' WHERE id=?", (sid,))
    render.render_short(library, settings, channel, sid)
    assert library.execute("SELECT status FROM shorts WHERE id=?", (sid,)).fetchone()[0] == "approved"


def test_short_scene_holds_last_frame(library, settings, channel, monkeypatch):
    """A block longer than its scene must not run on past the cut."""
    from shorts import media
    fake_claude(monkeypatch)
    sid = pipeline.make(library, settings, channel, count=1)[0]
    first = library.execute("SELECT scene_id, seconds FROM blocks WHERE short_id=? AND idx=0", (sid,)).fetchone()
    library.execute("UPDATE scenes SET end = start + 1.0 WHERE id=?", (first["scene_id"],))
    library.commit()
    out = render.render_short(library, settings, channel, sid)
    total = library.execute("SELECT SUM(seconds) FROM blocks WHERE short_id=?", (sid,)).fetchone()[0]
    assert abs(media.probe(out)["duration"] - total) < 0.3


def test_duplicate_scenes_chunked_matches_full():
    import sqlite3
    from shorts import db
    conn = db.connect(":memory:")
    rng = np.random.default_rng(1)
    base = rng.standard_normal((6, 8)).astype(np.float32)
    vecs = np.concatenate([base, base + 0.001])
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    for i, v in enumerate(vecs):
        aid = conn.execute("INSERT INTO assets (source, source_id, license) VALUES ('t', ?, 'pd')", (str(i),)).lastrowid
        conn.execute("INSERT INTO scenes (asset_id, start, end, embedding) VALUES (?, 0, 1, ?)", (aid, v.tobytes()))
    assert dedupe.reject_duplicate_scenes(conn, chunk=4) == 6


def test_search_cache_follows_rejections(library, settings):
    first = embed.search(library, settings, "saturn", k=1)[0][0]
    library.execute("UPDATE scenes SET rejected=1 WHERE id=?", (first,))
    library.commit()
    assert first not in [s for s, _ in embed.search(library, settings, "saturn", k=100)]
