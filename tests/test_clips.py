"""meta/clips.py: the pure helpers and one real render (no network, no Claude)."""
import subprocess

from meta import clips
from meta.runner import command


def test_length_and_year():
    assert clips.length_seconds("01:02:03") == 3723
    assert clips.length_seconds("95.5") == 95.5
    assert clips.length_seconds("n/a") is None
    assert clips.year_of({"year": "1920"}) == 1920
    assert clips.year_of({"date": "1955-03-01"}) == 1955
    assert clips.year_of({}) is None


def test_pick_file_prefers_h264_derivative():
    files = [{"name": "a.ogv", "size": "5"}, {"name": "a_512kb.mp4", "format": "512Kb MPEG4", "size": "100"},
             {"name": "a.mpeg", "size": "9"}, {"name": "a.txt"}]
    assert clips.pick_file(files)[0] == "a_512kb.mp4"
    assert clips.pick_file([{"name": "a.txt"}]) is None
    assert clips.pick_file([{"name": "big.mp4", "size": str(3_000_000_000)}]) is None


def test_runner_allows_clips():
    assert command({"id": "2026-11-09-clips-1", "name": "clips"})[-1] == "meta.clips"


def test_gov_maker():
    assert clips.gov_maker({"creator": "U.S. Air Force"})
    assert not clips.gov_maker({"creator": "University of Kansas", "title": "Air Force ejection seat"})
    assert not clips.gov_maker({"title": "Ejection seat research"})
    assert clips.gov_maker({"creator": "Lookout Mountain Laboratory, 1352nd Photographic Group"})


def test_edited_has_no_visual_tricks():
    plan = {"start": 2.0, "end": 22.0, "setup_end": 8.0, "peak": 14.0, "replay": True}
    assert clips.timeline(plan, True, False) == [{"kind": "play", "t0": 2.0, "t1": 22.0, "speed": 1.0}]


def test_timeline_and_times(monkeypatch):
    monkeypatch.setattr(clips, "VISUAL_TRICKS", True)
    plan = {"start": 2.0, "end": 22.0, "setup_end": 8.0, "peak": 14.0, "replay": True}
    assert clips.timeline(plan, False, False) == [{"kind": "play", "t0": 2.0, "t1": 22.0, "speed": 1.0}]
    ed = clips.timeline(plan, True, False)
    assert [p["kind"] for p in ed] == ["play", "play", "freeze", "play", "replay"]
    assert ed[0]["speed"] == clips.SPEED_UP
    assert abs(clips.out_time(ed, 8.0) - 6 / clips.SPEED_UP) < 1e-6
    assert clips.out_time(ed, 15.0) > clips.out_time(ed, 14.0) + clips.FREEZE - 1e-6
    # no replay unless Claude marks the moment as worth seeing again
    assert "replay" not in [p["kind"] for p in clips.timeline(dict(plan, replay=False), True, False)]
    # dialogue: no speed-up, no replay
    assert [p["kind"] for p in clips.timeline(plan, True, True)] == ["play", "freeze", "play"]


def test_render_music_and_original(tmp_path):
    src = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=640x480:r=24:d=30",
                    "-f", "lavfi", "-i", "sine=d=30", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    plan = {"hook": "He fixed the roof and then THIS happened 😳",
            "captions": [{"at": 0, "text": "Watch the ladder", "highlight": ["ladder"]}, {"at": 5, "text": "Then the wind"}],
            "payoff": "A true fact as the payoff.", "credit": "Test film (1920)",
            "start": 2.0, "end": 17.0, "setup_end": 6.0, "peak": 11.0, "replay": True}
    for audio, edited in (("music", False), ("music", True), ("original", True)):
        out = tmp_path / f"{audio}-{edited}.mp4"
        total = clips.render(str(src), plan, audio, None, out, edited=edited)
        assert 14.9 < total < 15.5  # no speed-up, freeze or replay any more
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True)
        assert "video,1080,1920" in p.stdout and "audio" in p.stdout


def test_has_sound(tmp_path):
    loud, quiet = tmp_path / "loud.mp4", tmp_path / "quiet.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:r=24:d=3",
                    "-f", "lavfi", "-i", "sine=d=3", "-pix_fmt", "yuv420p", "-shortest", str(loud)], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:r=24:d=3",
                    "-pix_fmt", "yuv420p", str(quiet)], check=True)
    assert clips.has_sound(str(loud))
    assert not clips.has_sound(str(quiet))
