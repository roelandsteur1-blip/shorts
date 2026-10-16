from datetime import datetime, timezone

from shorts import render, script, upload
from shorts.config import list_channels
from shorts.ingest_nasa import needs_rights_check, pick_rendition
from shorts.llm import extract_json


def test_channels_configured():
    assert "space" in list_channels()


def test_split_block_respects_max_words():
    parts = script.split_block("one two three four five six seven eight nine ten eleven")
    assert all(len(p.split()) <= script.MAX_WORDS for p in parts)
    assert " ".join(parts).split() == "one two three four five six seven eight nine ten eleven".split()


def test_block_seconds_reading_speed():
    assert script.block_seconds("one two three") == script.MIN_BLOCK_SECONDS
    assert script.block_seconds("a b c d e f g h") >= 8 / 3


def test_normalise_stretches_to_minimum(channel):
    blocks = script.normalise_blocks([{"text": "Hi"}, {"text": "there", "visual": "x"}, {"text": "you", "visual": "y"}], channel)
    assert sum(b["seconds"] for b in blocks) >= channel.min_seconds
    assert blocks[0]["visual"] == "hero"


def test_extract_json_from_fenced_reply():
    assert extract_json('Sure!\n```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert extract_json('{"a": [1, 2]} trailing') == {"a": [1, 2]}


def test_rights_check_flags_third_party():
    assert needs_rights_check({"description": "Video courtesy of SpaceX"})
    assert needs_rights_check({"photographer": "ESA/NASA"})
    assert needs_rights_check({"title": "Rocket Lab Launches NASA's PREFIRE Mission"})
    assert not needs_rights_check({"description": "NASA's Perseverance rover on Mars", "center": "JPL"})


def test_pick_rendition_prefers_large():
    files = ["http://x/a~orig.mp4", "http://x/a~large.mp4", "http://x/a~mobile.mp4"]
    assert pick_rendition(files).endswith("~large.mp4")


def test_ass_highlight_and_timing():
    ass = render.build_ass([{"text": "Look at *this*", "seconds": 2.0}, {"text": "Next", "seconds": 1.5}], {})
    assert "Dialogue: 0,0:00:00.00,0:00:01.96,Hook" in ass
    assert "0:00:02.00,0:00:03.46,Body" in ass
    assert r"{\c&H0000D7FF}this{\c&H00FFFFFF}" in ass


def test_choose_mode():
    assert render.choose_mode(1920, 1080, 0.5) == "crop"
    assert render.choose_mode(1920, 1080, 0.05) == "blur"


def test_next_slots_respects_per_day(conn, channel):
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    slots = upload.next_slots(conn, channel, 3, now=now)
    assert [upload.iso(s) for s in slots] == ["2026-10-16T15:00:00Z", "2026-10-16T21:00:00Z", "2026-10-17T15:00:00Z"]


def test_upload_body():
    body = upload.body_for({"title": "T", "description": "D", "tags": ["a"]},
                           datetime(2026, 10, 17, 15, tzinfo=timezone.utc))
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": False, "publishAt": "2026-10-17T15:00:00Z"}
