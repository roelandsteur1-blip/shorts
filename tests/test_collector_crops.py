"""Crop research R6b (meta/collector/crops.py): clip choice, detector path and whole-frame
fallback, coverage, every method through the live renderer's filter, and the review grid."""
import subprocess

from meta.collector import crops


def test_pick_clips_keeps_checked_videos_without_black_or_text(tmp_path):
    (tmp_path / "space-saturn-A.mkv").write_bytes(b"x")
    (tmp_path / "space-saturn-B.mkv").write_bytes(b"x")
    (tmp_path / "space-saturn-C.mkv").write_bytes(b"x")
    made = [{"subjects": [{"niche": "space", "name": "Saturn", "candidates": [
        {"source": "library", "claude_shows": True, "preview": "space-saturn-A.mp4", "gates": {"faults": []}},
        {"source": "commons_video", "claude_shows": True, "preview": "space-saturn-B.mp4", "gates": {"faults": ["text on screen"]}},
        {"source": "nasa", "claude_shows": True, "preview": "space-saturn-C.mp4", "gates": {"faults": ["blurry"]}},
        {"source": "openverse", "kind": "photo", "claude_shows": True, "preview": "space-saturn-D.mp4", "gates": {"faults": []}},
        {"source": "commons_video", "claude_shows": False, "preview": "space-saturn-E.mp4", "gates": {"faults": []}}]}]}]
    got = crops.pick_clips(made, tmp_path)
    assert [c["preview"] for c in got] == ["space-saturn-C.mp4", "space-saturn-A.mp4"]


def test_detector_path_follows_the_box_and_shows_whole_when_too_wide():
    fw = 0.316  # 9:16 window in a 16:9 frame
    boxes = [(0.4, ([0.70, 0.3, 0.80, 0.6], 0.5)), (2.4, ([0.72, 0.3, 0.82, 0.6], 0.4)), (4.4, None)]
    p = crops.detector_path(boxes, fw)
    assert 0.74 < p["x0"] < 0.76 and 0.76 < p["x1"] < 0.78 and not p["whole"]
    wide = [(0.4, ([0.1, 0.3, 0.9, 0.6], 0.5))]
    assert crops.detector_path(wide, fw)["whole"]
    assert crops.detector_path([(0.4, ([0.1, 0.1, 0.2, 0.2], 0.05))], fw) is None


def test_coverage_and_reference_boxes():
    fw = 0.316
    ref = crops.reference_boxes({"owlv2": [(0.4, ([0.70, 0.3, 0.80, 0.6], 0.5))],
                                 "gdino": [(0.4, ([0.1, 0.3, 0.2, 0.6], 0.2))]})
    assert ref[0][1][1] == 0.5
    assert crops.coverage({"x0": 0.75, "x1": 0.75}, "crop", ref, fw) == 1.0
    assert crops.coverage({"x0": 0.5, "x1": 0.5}, "crop", ref, fw) == 0.0
    assert crops.coverage({}, "blur", ref, fw) == 1.0
    assert crops.coverage({}, "crop", [], fw) is None


def test_every_method_renders_and_the_grid_is_made(tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25:duration=8",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    panels = []
    frames = {"today": {"x0": 0.3, "x1": 0.4, "y0": 0.5, "y1": 0.5}, "centre": {"x0": 0.5, "x1": 0.5, "y0": 0.5, "y1": 0.5},
              "whole": {"whole": True}, "band": None}
    for lab, (m, f) in zip("ABCD", frames.items()):
        dst = tmp_path / f"{m}.mp4"
        crops.render(str(src), 8.0, 1280, 720, m, f, dst)
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(dst)],
                           capture_output=True, text=True)
        assert p.stdout.strip() == "1080,1920"
        panels.append((lab, dst))
    grid = tmp_path / "grid.mp4"
    crops.side_by_side(panels, grid)
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(grid)],
                       capture_output=True, text=True)
    assert p.stdout.strip() == f"{4 * crops.PANEL_W},{2 * crops.PANEL_H}"


def test_runner_allows_crops():
    from meta import runner
    cmd = runner.command({"id": "2026-11-05-crops-3", "name": "crops", "n": 2})
    assert "meta.collector.crops" in cmd and cmd[-2:] == ["--part", "2"]
