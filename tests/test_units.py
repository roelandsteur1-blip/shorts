from datetime import datetime, timezone

from shorts import render, script, upload
from shorts.config import list_channels
from shorts.sources.nasa import needs_rights_check, pick_rendition
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
    assert render.choose_mode(1920, 1080) == "crop"
    assert render.choose_mode(1080, 1920) == "crop"
    assert render.choose_mode(2560, 1080) == "blur"


def test_framing_finds_the_subject():
    import numpy as np
    from shorts import framing
    frames = []
    for i in range(4):  # a bright moving square on the left of a grey, slightly noisy frame
        f = np.full((216, 384, 3), 90, np.uint8)
        f += np.random.default_rng(i).integers(0, 6, f.shape, dtype=np.uint8)
        x = 40 + i * 6
        f[90:130, x:x + 40] = 240
        frames.append(f)
    res = framing.analyse(frames, 1920, 1080)
    fw, _ = framing.window_fraction(1920, 1080)
    assert res["x0"] < 0.3 and res["x1"] >= res["x0"]
    assert res["x0"] - fw / 2 <= 40 / 384  # the square is inside the window
    flat = framing.analyse([np.full((216, 384, 3), 90, np.uint8)] * 3, 1920, 1080)
    assert flat["x0"] == flat["x1"] == 0.5  # nothing stands out: keep the middle


def test_plan_motion():
    blocks = [{"text": "Hook", "scene_id": 1}, {"text": "same shot", "scene_id": 1},
              {"text": "a *big* word", "scene_id": 2}, {"text": "another *one*", "scene_id": 3},
              {"text": "plain", "scene_id": 4}, {"text": "*third*", "scene_id": 5},
              {"text": "payoff", "scene_id": 6}]
    plans = render.plan_motion(blocks)
    kinds = [p["kind"] for p in plans]
    assert kinds[0] == "push" and plans[0]["z1"] > 1.0
    assert plans[1]["z0"] == plans[0]["z1"]  # same shot carries on from the hook's zoom
    assert kinds[2] == "punch" and kinds[3] != "punch"  # never two punch-ins in a row
    assert kinds[5] == "punch" and kinds[6] == "push"
    assert all(1.0 <= p["z0"] <= render.MAX_ZOOM and 1.0 <= p["z1"] <= render.MAX_ZOOM for p in plans)


def test_next_slots_respects_per_day(conn, channel):
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    slots = upload.next_slots(conn, channel, 3, now=now)
    assert [upload.iso(s) for s in slots] == ["2026-10-16T15:00:00Z", "2026-10-16T21:00:00Z", "2026-10-17T15:00:00Z"]


def test_upload_body():
    body = upload.body_for({"title": "T", "description": "D", "tags": ["a"]},
                           datetime(2026, 10, 17, 15, tzinfo=timezone.utc))
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": False, "publishAt": "2026-10-17T15:00:00Z"}


def test_find_claude_in_store_app_folder(tmp_path, monkeypatch):
    """The Store (MSIX) desktop app keeps Claude Code under %LOCALAPPDATA%/Claude-Data."""
    from shorts import llm
    exe = tmp_path / "local" / "Claude-Data" / "claude_abc" / "claude-code" / "2.1.299" / "f0584be0fbaf" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert llm.find_claude("C:/gone/claude.exe") == str(exe)


def test_jobs_run_under_console_python(tmp_path, monkeypatch):
    """Dashboard under pythonw.exe: jobs still start python.exe, so claude/ffmpeg open no windows."""
    from shorts import jobs
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(jobs.sys, "executable", str(tmp_path / "pythonw.exe"))
    assert jobs.console_python() == str(tmp_path / "python.exe")
