"""Video type per channel: classic (the original) or a meta type (text card, quiz, ranking, mix)."""
import dataclasses
import shutil

import pytest

from shorts import channels as chans, metatype, pipeline, render
from shorts.config import load_channel
from shorts.web import create_app


@pytest.mark.parametrize("cid,niche", [
    ("space", "space"), ("historybuffed", "history"), ("body", "body"), ("earthquaked", "earthquake"),
    ("oceanfactoidss", "ocean"), ("oldcityfacts", "oldcity"), ("warplanedd", "warplane"),
    ("retrotechhh", "retrotech"), ("weatherwowed", "weather"), ("planetwildd", "planet")])
def test_topic_set_guessed_from_channel_id(cid, niche):
    assert metatype.guess_niche(cid, "") == niche
    assert metatype.guess_niche(cid, "", "ocean") == "ocean"  # picked by hand wins


def test_quiz_and_ranking_only_where_there_is_a_theme():
    assert metatype.available("space") == ["textcard", "quiz", "ranking"]
    assert metatype.available("body") == ["textcard"]
    assert metatype.available(None) == []


def test_existing_channels_are_classic(channel):
    assert channel.video_type == "classic" and not metatype.is_meta(channel)


def _scene(conn, title="Apollo 11 launch"):
    aid = conn.execute("INSERT INTO assets (source, source_id, title, license, status) VALUES ('nasa', ?, ?, 'PD', 'ok')",
                       (title, title)).lastrowid
    return conn.execute("INSERT INTO scenes (asset_id, start, end) VALUES (?, 0, 10)", (aid,)).lastrowid


def test_meta_channel_makes_finished_shorts_for_review(conn, settings, channel, monkeypatch, tmp_path):
    meta_ch = dataclasses.replace(channel, video_type="mix")
    scenes = [_scene(conn, f"clip {i}") for i in range(3)]
    video = tmp_path / "q.mp4"
    video.write_bytes(b"x")
    calls = []

    def fake_one(c, s, ch, niche, kind):
        calls.append(kind)
        if kind == "quiz" and calls.count("quiz") == 1:
            return []  # no footage this time: a quiz again with a new script, never another type
        return [{"kind": kind, "file": str(video), "title": "Can you name it?", "seconds": 12.0,
                 "text": "A surprising fact.", "scene_ids": scenes[:3], "music": None,
                 "rounds": [{"answer": f"clip {i}", "fact": "f"} for i in range(3)],
                 "items": [{"rank": i + 1, "name": f"clip {i}", "value": f"{i} m"} for i in range(3)]}]

    monkeypatch.setattr(metatype, "_one", fake_one)
    monkeypatch.setattr(metatype, "available", lambda niche: ["textcard", "quiz", "ranking"])
    conn.execute("INSERT INTO shorts (channel, format_id, status) VALUES ('space', 'meta_textcard', 'rejected')")
    waiting = pipeline.make_many(conn, settings, [(meta_ch, 2, None)])
    assert waiting == {"space": 2}
    assert calls == ["quiz", "quiz", "ranking"]  # mix: next after the last text card; a failed quiz stays a quiz
    rows = conn.execute("SELECT * FROM shorts WHERE status='rendered' ORDER BY id").fetchall()
    assert [r["format_id"] for r in rows] == ["meta_quiz", "meta_ranking"]
    sid = rows[0]["id"]
    assert conn.execute("SELECT COUNT(*) FROM blocks WHERE short_id=?", (sid,)).fetchone()[0] == 3
    meta = render.metadata(conn, channel, sid, settings)
    assert meta["title"] == "Can you name it?" and "clip 0" in meta["description"]
    # Never re-rendered from its blocks (Review's text edit, music re-render).
    assert render.render_short(conn, settings, channel, sid) == video


def test_describe_quiz_and_ranking():
    desc, blocks = metatype.describe({"kind": "ranking", "title": "t", "heading": "Ranked by size", "seconds": 18,
                                      "items": [{"rank": 1, "name": "Jupiter", "value": "142,984 km"},
                                                {"rank": 2, "name": "Saturn", "value": "120,536 km"}]})
    assert desc.startswith("Ranked by size\n1. Jupiter") and blocks[1] == ("2. Saturn (120,536 km)", 9.0)
    desc, blocks = metatype.describe({"kind": "quiz", "title": "t", "seconds": 21,
                                      "rounds": [{"answer": "Moon", "fact": "f"}] * 3})
    assert "Comment your score" in desc and len(blocks) == 3


def test_validate_video_type(channel):
    from shorts.channels import raw  # noqa: F401
    base = chans.new_channel("bodybuzzed", "Body Buzzed", "en", "the human body")
    assert chans.validate(dict(base, video_type="textcard"), set()) == []
    assert any("isn't available" in e for e in chans.validate(dict(base, video_type="quiz"), set()))
    assert any("unknown video type" in e for e in chans.validate(dict(base, video_type="nope"), set()))
    odd = chans.new_channel("abc", "Abc", "en", "x")
    assert any("topic set" in e for e in chans.validate(dict(odd, video_type="textcard"), set()))
    assert chans.validate(dict(odd, video_type="quiz", meta_niche="space"), set()) == []


def test_old_channel_files_get_classic(settings, tmp_path):
    from pathlib import Path
    cfg = tmp_path / "cfg"
    shutil.copytree(Path(chans.__file__).parent.parent / "config", cfg)
    text = (cfg / "channels" / "space.toml").read_text(encoding="utf-8")
    (cfg / "channels" / "space.toml").write_text(text.replace('video_type = "classic"\n', ""), encoding="utf-8")
    s = dataclasses.replace(settings, config_root=cfg)
    assert chans.add_video_types(s) == ["space"]
    assert load_channel("space", cfg).video_type == "classic"
    assert chans.add_video_types(s) == []


def test_channel_page_has_video_type(settings):
    client = create_app(settings, start_runner=False).test_client()
    page = client.get("/channels/space").data
    assert b"Video type" in page and b"Quiz:" in page and b"Automatic: Space" in page


def test_seed_channels_once(settings, tmp_path):
    from pathlib import Path
    cfg = tmp_path / "cfg"
    shutil.copytree(Path(chans.__file__).parent.parent / "config", cfg)
    for seed in (cfg / "channel-seeds").glob("*.toml"):  # a copy left in the repo's config by a dashboard run
        (cfg / "channels" / seed.name).unlink(missing_ok=True)
    s = dataclasses.replace(settings, config_root=cfg)
    added = chans.seed_channels(s)
    assert set(added) == {"historycards", "warplanecards", "namethatwarplane", "warplanesranked", "warplanemix",
                          "retrotechcards", "namethatgadget", "retrotechranked", "retrotechmix"}
    times = [t for cid in added for t in load_channel(cid, cfg).publish_times_utc]
    assert len(times) == len(set(times)) and all("17:00" <= t <= "22:00" for t in times)  # staggered slots
    for cid in added:
        ch = load_channel(cid, cfg)
        assert metatype.is_meta(ch) and chans.validate(chans.raw(s, cid), set()) == []
    (cfg / "channels" / "historycards.toml").unlink()  # deleted by hand: stays deleted
    assert chans.seed_channels(s) == []
    assert not (cfg / "channels" / "historycards.toml").exists()


def test_retire_channels(settings, tmp_path):
    from pathlib import Path
    from shorts import db
    cfg = tmp_path / "cfg"
    shutil.copytree(Path(chans.__file__).parent.parent / "config", cfg)
    s = dataclasses.replace(settings, config_root=cfg)
    (cfg / "channels" / "spaceranked.toml").write_text((cfg / "channels" / "space.toml").read_text())
    c = db.connect(s.db_path)
    for ch, st in [("spaceranked", "rendered"), ("spaceranked", "scheduled"), ("space", "rendered")]:
        c.execute("INSERT INTO shorts (channel, format_id, status) VALUES (?, 'f', ?)", (ch, st))
    c.commit()
    assert chans.retire_channels(s) == ["spaceranked"]
    assert not (cfg / "channels" / "spaceranked.toml").exists()
    assert (cfg / "channels" / "space.toml").exists()
    rows = c.execute("SELECT channel, status FROM shorts ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("spaceranked", "rejected"), ("spaceranked", "scheduled"),
                                         ("space", "rendered")]
    assert chans.retire_channels(s) == []


def test_meta_post_tags():
    assert metatype.post_tags({"kind": "quiz", "rounds": [{"answer": a} for a in "abcd"]}) == ["a", "b", "c"]
    assert metatype.post_tags({"kind": "textcard", "tags": ["x", "y"]}) == ["x", "y"]


def test_nichestats_runs_read_only(conn, settings, monkeypatch, capsys):
    import meta.nichestats as ns
    conn.execute("INSERT INTO yt_videos (youtube_id, channel, state, views) VALUES ('a','space','live',100)")
    conn.execute("INSERT INTO yt_videos (youtube_id, channel, state, views) VALUES ('b','space','live',300)")
    conn.commit()
    monkeypatch.setattr("shorts.config.load_settings", lambda *a, **k: settings)
    monkeypatch.setattr("shorts.config.all_channels", lambda *a, **k: [])
    ns.main()
    out = capsys.readouterr().out
    assert "space | space | - | 2 | 0 | 400 | 200" in out
    assert "space | space | 2 | 400 | 200 | 0 | yes" in out


def test_mix_takes_turns_with_classic(conn, settings, channel, monkeypatch):
    mix = dataclasses.replace(channel, video_type="mix")
    monkeypatch.setattr(metatype, "available", lambda niche: ["textcard", "quiz", "ranking"])
    assert metatype.mix_classic_count(conn, mix, 4) == 1  # nothing yet: classic first
    assert metatype.mix_classic_count(conn, mix, 8) == 2
    conn.execute("INSERT INTO shorts (channel, format_id, status) VALUES ('space', 'what_youre_seeing', 'rendered')")
    assert metatype.mix_classic_count(conn, mix, 3) == 0  # text card, quiz, ranking come first now
    conn.execute("INSERT INTO shorts (channel, format_id, status) VALUES ('space', 'meta_ranking', 'rendered')")
    assert metatype.mix_classic_count(conn, mix, 1) == 1
    monkeypatch.setattr(metatype, "available", lambda niche: ["textcard"])  # topic without quiz/ranking
    assert metatype.mix_classic_count(conn, mix, 4) == 2

    monkeypatch.setattr(metatype, "available", lambda niche: ["textcard", "quiz", "ranking"])
    made, lanes = [], []
    monkeypatch.setattr(metatype, "make", lambda c, s, ch, n: made.append(n))
    monkeypatch.setattr(pipeline, "run_steps", lambda c, s, ls, before_render=None: lanes.extend(ls))
    monkeypatch.setattr(pipeline, "_music_check", lambda *a: a[2].set())
    monkeypatch.setattr(pipeline, "_warm_up", lambda *a: None)
    pipeline.make_many(conn, settings, [(mix, 5, None)], restock=False)
    assert made == [3] and [(l.channel.id, l.count) for l in lanes] == [("space", 2)]
