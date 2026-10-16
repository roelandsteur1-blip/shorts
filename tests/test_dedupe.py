import subprocess

from conftest import make_video
from shorts import dedupe, media
from shorts.ingest_nasa import upsert_metadata


def test_normalise_title_strips_format_tags():
    assert dedupe.normalise_title("Artemis I Launch 4K") == dedupe.normalise_title("artemis_i_launch_1080p")
    assert dedupe.normalise_title("Saturn rings") != dedupe.normalise_title("Jupiter rings")


def test_same_title(conn):
    a = upsert_metadata(conn, {"nasa_id": "A", "title": "Perseverance Rover Landing Highlights 1080p",
                               "date_created": "2021-02-22T00:00:00Z"})
    b = upsert_metadata(conn, {"nasa_id": "B", "title": "Perseverance Rover Landing Highlights (4K)",
                               "date_created": "2021-02-22T00:00:00Z"})
    assert dedupe.same_title(conn, "Perseverance Rover Landing Highlights (4K)", b, "2021-02-22") == a
    # Same title, other day: a different event (NASA reuses titles), not a duplicate.
    assert dedupe.same_title(conn, "Perseverance Rover Landing Highlights", None, "2022-05-01") is None
    assert dedupe.same_title(conn, "Launch", None) is None  # too short to judge


def slow_video(path, sources=("testsrc2", "smptehdbars", "rgbtestsrc", "yuvtestsrc")):
    """Calm footage, closer to real NASA shots than the fast synthetic test patterns."""
    graph = "".join(f"{s}=s=1280x720:r=30:d=6[v{i}];" for i, s in enumerate(sources))
    graph += "".join(f"[v{i}]" for i in range(len(sources))) + f"concat=n={len(sources)}:v=1[out]"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-filter_complex", graph, "-map", "[out]",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


def test_fingerprint_matches_reencode_and_trim_not_other_footage(tmp_path):
    src = tmp_path / "a.mp4"
    slow_video(src)
    # Re-published copy: lower resolution, re-encoded, first 3 seconds trimmed.
    copy = tmp_path / "copy.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "3", "-i", str(src), "-vf", "scale=854:480",
                    "-c:v", "libx264", "-crf", "30", str(copy)], check=True)
    other = tmp_path / "b" / "b.mp4"
    other.parent.mkdir()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "cellauto=s=1280x720:rule=110",
                    "-t", "20", "-pix_fmt", "yuv420p", str(other)], check=True)
    fa, fc, fo = dedupe.fingerprint(str(src)), dedupe.fingerprint(str(copy)), dedupe.fingerprint(str(other))
    assert dedupe.similar(fa, fc)
    assert not dedupe.similar(fa, fo)
    # Same scenes in a different order (a montage) is not a re-upload.
    shuffled = tmp_path / "shuffled.mp4"
    slow_video(shuffled, ("yuvtestsrc", "rgbtestsrc", "smptehdbars", "testsrc2"))
    assert not dedupe.similar(fa, dedupe.fingerprint(str(shuffled)))


def test_dedupe_library_marks_later_copy(conn, settings, tmp_path):
    src = tmp_path / "v.mp4"
    slow_video(src)
    ids = []
    for n, path in enumerate([src, src]):
        aid = upsert_metadata(conn, {"nasa_id": f"X{n}", "title": f"Different title number {n}"})
        info = media.probe(path)
        conn.execute("UPDATE assets SET path=?, duration=?, status='downloaded' WHERE id=?",
                     (str(path), info["duration"], aid))
        ids.append(aid)
    conn.commit()
    res = dedupe.dedupe_library(conn)
    assert res["duplicate_videos"] == 1
    assert sorted(dict(conn.execute("SELECT id, status FROM assets").fetchall()).values()) == ["downloaded", "duplicate"]


def test_duplicate_scenes_across_videos_rejected(library):
    # Copy one scene's embedding onto a scene of the other video: that one must be rejected.
    a, b = library.execute("""SELECT MIN(id) FROM scenes WHERE rejected=0 GROUP BY asset_id""").fetchall()[:2]
    emb = library.execute("SELECT embedding FROM scenes WHERE id=?", (a[0],)).fetchone()[0]
    library.execute("UPDATE scenes SET embedding=? WHERE id=?", (emb, b[0]))
    library.commit()
    assert dedupe.reject_duplicate_scenes(library) >= 1
    assert library.execute("SELECT rejected FROM scenes WHERE id=?", (b[0],)).fetchone()[0] == 1
