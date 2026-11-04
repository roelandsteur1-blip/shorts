"""Footage collector research R4-R7 (meta/collector/pieces.py): subject split, blind labels, the
fault rules, the 9:16 preview with real ffmpeg, a real piece cut from a local file, and that one
failing candidate or source never stops a subject (R7)."""
import random
import subprocess

from PIL import Image

from meta.collector import pieces, sweep


def test_parts_split_30_subjects_3_per_niche():
    one, two = pieces.subjects_for(1), pieces.subjects_for(2)
    assert len(one) == len(two) == 15
    assert {s["niche"] for s in one} == set(pieces.NICHES[:5]) and {s["niche"] for s in two} == set(pieces.NICHES[5:])
    assert not {s["name"] for s in one} & {s["name"] for s in two}


def test_labels_are_unique_and_hide_the_source():
    labs = pieces.labels(9, random.Random(1))
    assert len(set(labs)) == 9 and all(len(x) == 1 for x in labs)


def test_retrotech_uses_only_roelands_sources():
    assert {s for s, _ in pieces.SOURCES["retrotech"]} == {"library", "openverse"}
    assert ("nasa", 2) in pieces.SOURCES["space"] and ("nasa", 2) not in pieces.DEFAULT


def test_fault_rules():
    assert pieces.faults({"brightness": 5, "motion": 0.1, "text_share": 0.2, "sharpness": 3}) == \
        ["black", "frozen", "text on screen", "blurry"]
    assert pieces.faults({"brightness": 100, "motion": 3, "text_share": 0.0, "sharpness": 300}) == []
    assert pieces.faults({"text_share": None}) == []


def _video(path, seconds=12):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=25:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def test_piece_from_a_local_file_and_vertical_preview(tmp_path):
    src = tmp_path / "src.mp4"
    _video(src)
    c = {"source": "library", "file": str(src), "start": 2.0, "label": "A"}
    out = pieces.fetch_video(c, {"name": "x", "show": "y"}, None, tmp_path)
    assert 7.5 <= pieces.duration(str(out)) <= 8.5 and c["fetch"]["method"] == "library"
    frames = pieces.clip_frames(out, 8.0)
    assert len(frames) == 3 and frames[0].height == 360
    g = pieces.Gates(None).check(frames)
    assert "black" not in g["faults"] and g["saturation"] > 0
    dst = tmp_path / "v.mp4"
    pieces.vertical(out, dst)
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(dst)],
                       capture_output=True, text=True)
    assert p.stdout.strip() == "540,960"


def test_one_failing_candidate_or_source_does_not_stop_the_subject(tmp_path, monkeypatch):
    monkeypatch.setattr(pieces, "OUT", tmp_path)
    src = tmp_path / "src.mp4"
    _video(src)
    monkeypatch.setattr(pieces, "candidates", lambda s, lib, scorer, keys: [
        {"source": "commons_video", "error": "search: archive down"},
        {"source": "commons_video", "ok": True, "file": "https://example.invalid/x.mp4", "bytes": 1000},
        {"source": "library", "ok": True, "file": str(src), "start": 1.0}])

    def boom(url, dst, limit=0):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(pieces, "download", boom)
    seen = []
    row = pieces.run_subject({"niche": "space", "name": "Saturn", "show": "planet"}, None, None, pieces.Gates(None),
                             {}, tmp_path, random.Random(2), lambda items: seen.extend(items) or {items[0][0]})
    c = row["candidates"]
    assert "archive down" in c[0]["error"] and "connection reset" in c[1]["error"]
    assert "gates" in c[2] and c[2]["claude_shows"] is True and c[2]["preview"].startswith("space-saturn-")
    assert len(seen) == 1 and (tmp_path / "clips" / c[2]["preview"]).exists()
    summ = pieces.summary([row])
    assert summ["commons_video"]["failed"] == 2 and summ["library"]["made"] == 1


def test_runner_allows_pieces_with_a_part_number():
    from meta import runner
    assert runner.command({"id": "2026-11-05-pieces-1", "name": "pieces", "n": 2})[-2:] == ["--part", "2"]
    assert runner.command({"id": "2026-11-05-pieces-9", "name": "pieces", "n": 3}) is None
