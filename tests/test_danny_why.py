"""Roeland's picks from research/danny-why-lessons.md (2026-11-08): mini quests A/B, the bottom
quarter on Stats, the 'Can we...?' format and the Pause button."""
import dataclasses
from datetime import date

from test_finishing import _age
from test_schedule_stats import _FakeYouTube, add_short, cfg, fake_upload  # noqa: F401 - fixtures
from shorts import channels as chans, learn, pipeline, script, upload
from shorts.config import load_channel


def test_quest_split_is_half_and_half():
    for n in range(1, 8):
        split = script.quest_split(n)
        assert len(split) == n and abs(sum(split) - (n - sum(split))) <= 1


def test_prompt_marks_each_hero_middle(channel):
    hero = {"archive": "NASA", "title": "t", "start": 0.0, "end": 4.0}
    text = script.build_prompt(channel, channel.formats[0], [dict(hero, quests=True), dict(hero, quests=False)], [])
    assert "Middle: MINI QUESTS" in text and "Middle: ONE THREAD" in text and "MINI QUESTS: also give" in text






def test_can_we_format_is_offered_to_every_classic_mix(channel):
    from shorts import formats
    assert "can_we" in [f["id"] for f in formats.for_channel(channel)]
    assert "can_we" not in [f["id"] for f in formats.for_channel(dataclasses.replace(channel, library_formats=False))]


def test_paused_channel_is_skipped_by_make_all_and_daily_upload(conn, settings, cfg, fake_upload):  # noqa: F811
    space = cfg / "channels" / "space.toml"
    (cfg / "channels" / "body.toml").write_text(space.read_text().replace('id = "space"', 'id = "body"'))
    chans.set_paused(settings, "body", True)
    body = load_channel("body", settings.config_dir)
    assert body.paused and not load_channel("space", settings.config_dir).paused
    orders, enough = pipeline.top_up_orders(conn, [load_channel("space", settings.config_dir), body], 1)
    assert [ch.id for ch, _, _ in orders] == ["space"] and any("(paused)" in e for e in enough)
    fake_upload(_FakeYouTube(ok=100))
    add_short(conn, video_path="x.mp4")
    add_short(conn, video_path="x.mp4", channel="body")
    out = upload.upload_all(conn, settings)
    assert "body" not in out and out["space"].startswith("uploaded 1")
    assert conn.execute("SELECT status FROM shorts WHERE channel='body'").fetchone()[0] == "approved"
    chans.set_paused(settings, "body", False)
    assert not load_channel("body", settings.config_dir).paused
    assert "paused" not in chans.raw(settings, "body")




