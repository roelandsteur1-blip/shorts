"""Fixes from the code review of 16 Oct 2026."""
import json
import re

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


def test_off_niche_hero_is_skipped_and_remembered(library, settings, channel, monkeypatch):
    heroes = hero.candidates(library, settings, channel.id, 2)
    reply = [{"hero": 1, "skip": "a steel mill, not space"},
             {"hero": 2, "topic": "two", "blocks": [{"text": "a b c"}] * 4}]
    monkeypatch.setattr(llm, "ask", lambda *a, **k: json.dumps(reply))
    created = script.write_scripts(library, settings, channel, "what_youre_seeing", heroes)
    assert len(created) == 1
    skipped = library.execute("SELECT asset_id FROM scenes WHERE id=?", (heroes[0],)).fetchone()[0]
    assert library.execute("SELECT reason FROM channel_fit WHERE fits=0 AND channel=? AND asset_id=?",
                           (channel.id, skipped)).fetchone()[0] == "a steel mill, not space"
    again = hero.candidates(library, settings, channel.id, 10)
    assert skipped not in {library.execute("SELECT asset_id FROM scenes WHERE id=?", (h,)).fetchone()[0] for h in again}



def test_videos_sorted_per_channel_niche(library, settings, channel, monkeypatch):
    """One call sorts the library for the channel; only fitting videos give hero shots, answers
    stick, a changed niche sorts again, and a correction by hand wins."""
    from shorts import fit
    library.execute("DELETE FROM channel_fit")
    assets = [r[0] for r in library.execute("SELECT id FROM assets ORDER BY id")]
    bad = assets[0]
    calls = []

    def ask(prompt, *a, **k):
        calls.append(prompt)
        assert "You sort public-domain archive videos" in prompt and "space: " in prompt
        n = len(re.findall(r"^\d+\. ", prompt, re.M))
        return json.dumps([{"n": i + 1, "channels": [] if i == 0 else ["space"]} for i in range(n)])
    monkeypatch.setattr(llm, "ask", ask)
    assert fit.sort_pending(library, settings, [channel]) == len(assets)
    assert len(calls) == 1
    picked = hero.candidates(library, settings, channel.id, 10, fits=channel)
    used = {library.execute("SELECT asset_id FROM scenes WHERE id=?", (h,)).fetchone()[0] for h in picked}
    assert picked and bad not in used
    assert bad not in {library.execute("SELECT asset_id FROM scenes WHERE id=?", (s,)).fetchone()[0]
                       for s in fit.allowed_scenes(library, channel)}
    assert fit.sort_pending(library, settings, [channel]) == 0 and len(calls) == 1
    # By hand: the rejected video is used after all, also after the niche changes.
    fit.mark(library, channel, bad, True)
    monkeypatch.setattr(channel, "niche", "something else")
    fit.sort_pending(library, settings, [channel])
    assert len(calls) == 2 and len(re.findall(r"^\d+\. ", calls[1], re.M)) == len(assets) - 1
    assert library.execute("SELECT fits FROM channel_fit WHERE channel=? AND asset_id=?",
                           (channel.id, bad)).fetchone()[0] == 1


def test_old_off_niche_rows_move_to_channel_fit(tmp_path):
    from shorts import db
    c = db.connect(tmp_path / "x.db")
    c.execute("INSERT INTO off_niche (channel, asset_id, reason) VALUES ('body', 7, 'a flood')")
    c.commit()
    c.close()
    c = db.connect(tmp_path / "x.db")
    assert c.execute("SELECT fits, niche, method FROM channel_fit WHERE channel='body' AND asset_id=7").fetchone()[:] == (0, "*", "script")
    assert c.execute("SELECT COUNT(*) FROM off_niche").fetchone()[0] == 0
