"""Whole 'make' step on synthetic footage with fake CLIP and fake Claude, real ffmpeg render."""
import json

from conftest import fake_claude
from shorts import media, pipeline
from shorts.web import create_app


def test_scenes_and_audio_stripped(library):
    n = library.execute("SELECT COUNT(*) FROM scenes WHERE rejected=0 AND embedding IS NOT NULL").fetchone()[0]
    assert n >= 4
    for (path,) in library.execute("SELECT path FROM assets"):
        assert not media.probe(path)["has_audio"]


def test_make_renders_reviewable_short(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    done = pipeline.make(library, settings, channel, count=2)
    assert len(done) == 2

    s = library.execute("SELECT * FROM shorts WHERE id=?", (done[0],)).fetchone()
    info = media.probe(s["video_path"])
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["has_audio"]
    assert channel.min_seconds - 0.5 <= info["duration"] <= channel.max_seconds + 1
    assert s["factcheck_ok"] == 0  # one fact flagged 'unclear'
    assert "NASA (public domain)" in s["description"]
    # Sources are named, not linked (links in a Shorts description are not clickable).
    assert "Sources: NASA Image and Video Library, NASA Science" in s["description"]
    assert "http" not in s["description"]

    # Two Shorts never open with the same source video.
    heroes = library.execute("""SELECT sc.asset_id FROM shorts s JOIN scenes sc ON sc.id=s.hero_scene_id""").fetchall()
    assert len({h[0] for h in heroes}) == 2

    app = create_app(settings, start_runner=False)
    client = app.test_client()
    assert b"Saturn" in client.get("/review").data
    assert b"Saturn" not in client.get("/review?channel=other").data
    page = client.get(f"/short/{done[0]}").data
    assert b"unclear" in page and b"Approve" in page
    assert client.get(f"/video/{done[0]}").status_code == 200
    client.post(f"/short/{done[0]}", data={"action": "approve", "title": "New title", "tags": "a, b"})
    s = library.execute("SELECT status, title, tags FROM shorts WHERE id=?", (done[0],)).fetchone()
    assert (s["status"], s["title"], json.loads(s["tags"])) == ("approved", "New title", ["a", "b"])


def test_music_is_mixed(library, settings, channel, monkeypatch):
    import subprocess
    (settings.music_dir / "space").mkdir()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=5",
                    str(settings.music_dir / "space" / "track.mp3")], check=True)
    fake_claude(monkeypatch)
    done = pipeline.make(library, settings, channel, count=1)
    s = library.execute("SELECT music FROM shorts WHERE id=?", (done[0],)).fetchone()
    assert s["music"].endswith("track.mp3")
