"""Roeland's picks from research/danny-why-lessons.md (2026-11-08): mini quests A/B, the bottom
quarter on Stats, the 'Can we...?' format and the Pause button."""
import dataclasses
from datetime import date

from test_finishing import _age
from test_schedule_stats import _FakeYouTube, add_short, cfg, client, fake_upload  # noqa: F401 - fixtures
from shorts import channels as chans, learn, pipeline, script, upload
from shorts.config import load_channel
from shorts.web import stats as stats_page


def test_quest_split_is_half_and_half():
    for n in range(1, 8):
        split = script.quest_split(n)
        assert len(split) == n and abs(sum(split) - (n - sum(split))) <= 1


def test_prompt_marks_each_hero_middle(channel):
    hero = {"archive": "NASA", "title": "t", "start": 0.0, "end": 4.0}
    text = script.build_prompt(channel, channel.formats[0], [dict(hero, quests=True), dict(hero, quests=False)], [])
    assert "Middle: MINI QUESTS" in text and "Middle: ONE THREAD" in text and "MINI QUESTS: also give" in text


def test_quest_flag_is_saved_and_scored(conn):
    ids = [add_short(conn, status="scheduled", quests=q) for q in (1, 1, 1, 0, 0, 0)]
    for sid, v in zip(ids, [400, 400, 400, 100, 100, 100]):
        _age(conn, sid, 3, v, v)
    rows = stats_page.quest_test(conn)
    assert [r["quests"] for r in rows] == [True, False]
    assert rows[0]["score"].startswith("+") and rows[1]["score"].startswith("-")
    assert {s["quests"] for s in learn.short_scores(conn)} == {0, 1}


def test_compare_shows_bottom_quarter_and_share_over_1000(conn):
    for i, v in enumerate([100, 200, 1500, 5000]):
        _age(conn, add_short(conn, status="scheduled", publish_at=f"2026-10-1{i}T15:00:00Z"), 1, v, v // 2)
    row = stats_page.compare_channels(conn, 1, today=date(2026, 10, 17))[0]
    assert row["floor"] == 175 and row["steady"] == 0.5
    assert stats_page.floor([]) == 0.0 and stats_page.floor([7]) == 7


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


def test_pause_button_on_home_and_channel_page(client, settings, cfg):  # noqa: F811
    home = client.get("/").data.decode()
    assert 'action="/channels/space/pause"' in home and ">Pause<" in home
    r = client.post("/channels/space/pause", data={"paused": "1"})
    assert r.status_code == 302 and "paused" in r.headers["Location"]
    assert load_channel("space", settings.config_dir).paused
    home = client.get("/").data.decode()
    assert ">Resume<" in home and "Paused</span>" in home
    page = client.get("/channels/space").data.decode()
    assert "Paused: Make for every channel" in page
    r = client.post("/channels/space/pause", data={"paused": "0", "back": "/channels/space"})
    assert r.headers["Location"].endswith("/channels/space?saved=1")
    assert not load_channel("space", settings.config_dir).paused


def test_running_and_paused_filter_chips(client, settings, cfg):  # noqa: F811
    assert "chiplabel\">State" not in client.get("/").data.decode()  # nothing paused: no State chips
    space = cfg / "channels" / "space.toml"
    (cfg / "channels" / "body.toml").write_text(space.read_text().replace('id = "space"', 'id = "body"'))
    chans.set_paused(settings, "body", True)
    page = client.get("/").data.decode()
    assert "chiplabel\">State" in page and "Paused <span>1</span>" in page and "Running <span>1</span>" in page
    page = client.get("/?f=1&state=paused").data.decode()
    assert 'action="/channels/body/pause"' in page and 'action="/channels/space/pause"' not in page
    page = client.get("/").data.decode()  # remembered in the cookie
    assert 'action="/channels/space/pause"' not in page
