"""Speed plan: Quiet/Fast switch, timings, lean Claude calls, usage-limit pause, one-pass scenes,
overlapping make."""
import json
import os
import stat
import sys

import pytest

from conftest import fake_claude, make_video
from shorts import llm, media, pipeline, scenes, speed, timing
from shorts.web import create_app


def test_speed_mode_and_limits(settings):
    assert speed.get_mode(settings.data_dir) == "quiet"
    quiet = speed.limits(settings.data_dir)
    assert quiet.render_workers == 1 and quiet.x264_threads >= 2
    speed.set_mode(settings.data_dir, "fast")
    fast = speed.limits(settings.data_dir, nvenc=True)
    assert fast.mode == "fast" and fast.render_workers == 3 and fast.scene_workers >= quiet.scene_workers
    gate = speed.Gate(settings.data_dir, "render_workers")
    assert gate.allowed() == 2
    speed.set_mode(settings.data_dir, "quiet")
    assert gate.allowed() == 1  # a running job sees the switch on its next render
    with pytest.raises(ValueError):
        speed.set_mode(settings.data_dir, "turbo")
    speed.lower_priority()  # never raises


def test_speed_switch_on_dashboard(settings):
    client = create_app(settings, start_runner=False).test_client()
    assert b'value="fast"' in client.get("/").data
    client.post("/speed", data={"mode": "fast"})
    assert speed.get_mode(settings.data_dir) == "fast"


def test_timings_and_claude_usage(conn):
    with timing.timed(conn, "render", short=1) as extra:
        extra["encoder"] = "libx264"
    timing.record(conn, "claude script", 12.0, usage={"input_tokens": 100, "output_tokens": 50,
                                                       "cache_read_input_tokens": 1000})
    stages = {t["stage"]: t for t in timing.summary(conn)}
    assert set(stages) == {"render", "claude script"}
    assert json.loads(conn.execute("SELECT info FROM timings WHERE stage='render'").fetchone()[0])["encoder"] == "libx264"
    assert timing.claude_usage(conn) == {"calls": 1, "input": 100, "output": 50, "cached": 1000}


@pytest.fixture
def fake_claude_exe(tmp_path, monkeypatch):
    """A stand-in 'claude' executable: --help lists some flags; -p answers from REPLY."""
    if sys.platform == "win32":
        pytest.skip("shell script stand-in")
    exe = tmp_path / "claude"
    exe.write_text(f"""#!{sys.executable}
import json, os, sys
if "--help" in sys.argv:
    print("  --tools <tools...>\\n  --strict-mcp-config\\n  --system-prompt <prompt>")
    sys.exit(0)
open(os.environ["ARGS_LOG"], "w").write(json.dumps(sys.argv[1:]))
sys.stdin.read()
print(os.environ["REPLY"])
""")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ARGS_LOG", str(tmp_path / "args.json"))
    monkeypatch.setattr(llm, "_limit_hit", None)
    llm._flag_cache.clear()
    return exe


def test_lean_flags_and_usage_logged(fake_claude_exe, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("REPLY", json.dumps({"result": "[1]", "usage": {"input_tokens": 7, "output_tokens": 3}}))
    assert llm.ask_json("hi", str(fake_claude_exe), conn=conn, stage="claude script") == [1]
    args = json.loads((tmp_path / "args.json").read_text())
    # Only the flags this version lists, and never --bare (it skips the subscription login).
    assert args[:3] == ["-p", "--output-format", "json"]
    assert "--tools" in args and "--strict-mcp-config" in args and "--system-prompt" in args
    assert "--disable-slash-commands" not in args and "--bare" not in args
    assert timing.claude_usage(conn)["input"] == 7


def test_usage_limit_pauses_claude(fake_claude_exe, monkeypatch):
    monkeypatch.setenv("REPLY", json.dumps({"is_error": True, "result": "Claude usage limit reached. Resets at 3pm"}))
    with pytest.raises(llm.LLMLimitError):
        llm.ask_json("hi", str(fake_claude_exe))
    assert llm.limit_reached()
    os.remove(os.environ["ARGS_LOG"])
    with pytest.raises(llm.LLMLimitError):
        llm.ask("again", str(fake_claude_exe))
    assert not os.path.exists(os.environ["ARGS_LOG"])  # not even called again


def test_fast_scene_detector_finds_the_cuts(tmp_path):
    video = tmp_path / "v.mp4"
    make_video(video, seconds=24)  # hard cut every 6 s
    info = media.probe(video)
    found = scenes.analyse_range(1, str(video), 0, info["duration"], info["width"], info["height"],
                                 str(tmp_path / "kf"), 10, "cpu")
    starts = [round(f["start"]) for f in found]
    assert starts == [0, 6, 12, 18]
    assert all(os.path.exists(f["keyframe"]) for f in found)
    # A long video is split over workers; a range edge is a cut, the rest stays the same.
    parts = [f for a, b in [(0, 10), (10, info["duration"])] for f in
             scenes.analyse_range(1, str(video), a, b, info["width"], info["height"], str(tmp_path / "kf2"), 10, "cpu")]
    assert [round(f["start"]) for f in parts] == [0, 6, 10, 12, 18]
    assert scenes.ranges_for(100) == [(0.0, 100)]
    assert len(scenes.ranges_for(1800)) == 10


def test_limit_mid_make_still_renders_written_scripts(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    real = llm.ask
    calls = {"script": 0}

    def ask(prompt, claude_cmd="claude", timeout=600):
        if "fact checker" not in prompt:
            calls["script"] += 1
            if calls["script"] > 1:
                raise llm.LLMLimitError("Claude usage limit reached")
        return real(prompt, claude_cmd)
    monkeypatch.setattr(llm, "ask", ask)
    # 2 hero shots, scripts in batches of 1: the first is written, checked and rendered; the second
    # hero is left for the next run instead of failing.
    orig = pipeline.script.write_scripts
    monkeypatch.setattr(pipeline.script, "write_scripts", lambda *a, **kw: orig(*a, **{**kw, "batch_size": 1}))
    done = pipeline.make(library, settings, channel, count=2)
    assert len(done) == 1
    assert library.execute("SELECT COUNT(*) FROM shorts").fetchone()[0] == 1
