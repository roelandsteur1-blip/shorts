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


def test_render_music_and_original(tmp_path):
    src = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=640x480:r=24:d=30",
                    "-f", "lavfi", "-i", "sine=d=30", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    plan = {"hook": "He fixed the roof and then THIS happened 😳",
            "captions": [{"at": 0, "text": "Watch the ladder"}, {"at": 5, "text": "Then the wind"}],
            "payoff": "A true fact as the payoff.", "credit": "Test film (1920)"}
    for audio in ("music", "original"):
        out = tmp_path / f"{audio}.mp4"
        clips.render(str(src), 2, 15, plan, audio, None, out)
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True)
        assert "video,1080,1920" in p.stdout and "audio" in p.stdout
