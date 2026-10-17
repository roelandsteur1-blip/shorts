"""Background music: fetching CC0 tracks, moods, picking, claims, Music page."""
import subprocess

import numpy as np
import pytest

from shorts import music
from shorts.web import create_app


def tone(path, seconds=40, silence=0.0, freq=330):
    """An mp3 with `silence` seconds of quiet before the tone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    src = f"sine=f={freq}:d={seconds}"
    filt = ["-af", f"adelay={int(silence * 1000)}:all=1,atrim=0:{seconds}"] if silence else []
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", src, *filt, str(path)], check=True)
    return path


class FakeClap:
    """Every track sounds like `kind` in `mood`."""
    def __init__(self, mood="epic", kind=music.KIND[0]):
        self.mood, self.kind = mood, kind

    def audio(self, waves):
        return np.array([[1.0, 0.0]])

    def texts(self, texts):
        return np.array([[1.0, 0.0] if t in (music.MOODS[self.mood], self.kind) else [0.0, 1.0] for t in texts])


def short(conn, channel="space", music_path=None, mood=None, status="rendered", youtube_id=None):
    cur = conn.execute("INSERT INTO shorts (channel, format_id, status, music, mood, youtube_id) VALUES (?,?,?,?,?,?)",
                       (channel, "f", status, music_path, mood, youtube_id))
    conn.commit()
    return cur.lastrowid


def test_music_start_skips_quiet_intro(tmp_path):
    p = tone(tmp_path / "t.mp3", seconds=40, silence=6)
    start = music.music_start(music.decode(p))
    assert 5.0 <= start <= 6.5
    assert music.music_start(music.decode(tone(tmp_path / "u.mp3", seconds=40))) == 0.0


def test_pick_mood_least_used_and_not_previous(conn, settings, channel):
    d = settings.music_dir / "space"
    paths = [str(tone(d / f"{n}.mp3", seconds=3, freq=200 + i * 50)) for i, n in enumerate(["a", "b", "c"])]
    music.sync(conn, settings)
    rows = {r["title"]: r["id"] for r in conn.execute("SELECT id, title FROM tracks")}
    music.set_mood(conn, rows["a"], "epic")
    music.set_mood(conn, rows["b"], "epic")
    music.set_mood(conn, rows["c"], "calm")

    short(conn, music_path=paths[0])                      # a used once, and the previous Short had it
    sid = short(conn, mood="epic")
    assert music.pick(conn, settings, sid, channel)["title"] == "b"
    sid = short(conn, mood="calm")
    assert music.pick(conn, settings, sid, channel)["title"] == "c"
    sid = short(conn, mood="tense")                       # no tense track: any mood, least used
    assert music.pick(conn, settings, sid, channel)["title"] in ("b", "c")


def test_fetch_keeps_only_cc0_music(conn, settings, channel, tmp_path):
    files = {"good": tone(tmp_path / "src" / "good.mp3"), "short": tone(tmp_path / "src" / "short.mp3", seconds=10)}

    class Resp:
        def __init__(self, data=None, body=b"", status=200):
            self.data, self.body, self.status_code = data, body, status
        def json(self): return self.data
        def raise_for_status(self): pass
        def iter_content(self, n): yield self.body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    calls = []

    class Session:
        def get(self, url, params=None, **kw):
            calls.append(url)
            if url == music.OPENVERSE:
                if params["page"] > 1:
                    return Resp(status=400)
                return Resp({"results": [
                    {"id": "1", "title": "Good space ambient", "license": "cc0", "url": "https://x/good.mp3", "duration": 40000},
                    {"id": "2", "title": "Needs credit", "license": "by", "url": "https://x/by.mp3", "duration": 40000},
                    {"id": "3", "title": "Jingle", "license": "pdm", "url": "https://x/short.mp3", "duration": 10000},
                    {"id": "4", "title": "Podcast", "license": "cc0", "url": "https://x/good.mp3", "duration": 40000},
                ]})
            return Resp(body=files["good"].read_bytes())

    channel.music_terms = ["space ambient"]
    clap = FakeClap("mysterious")
    music.set_tagger(clap)
    real = music.add_fetched

    def add(conn_, settings_, fold, hit, s):  # the 'podcast' sounds like people talking
        clap.kind = "people talking" if hit["title"] == "Podcast" else music.KIND[0]
        return real(conn_, settings_, fold, hit, s)

    music.add_fetched = add
    try:
        assert music.fetch(conn, settings, channel, want=5, session=Session()) == 1
    finally:
        music.add_fetched = real
    ok = music.usable(conn, "space")
    assert [(t["title"], t["license"], t["mood"]) for t in ok] == [("Good space ambient", "CC0", "mysterious")]
    assert any((settings.music_dir / "space").glob("openverse-1-*.mp3"))
    notes = dict(conn.execute("SELECT title, note FROM tracks WHERE status='rejected'").fetchall())
    assert "length" in notes["Jingle"] and "talking" in notes["Podcast"]
    assert "Needs credit" not in notes  # never even stored: wrong licence
    downloads = sum(1 for c in calls if c.startswith("https://x/"))
    music.fetch(conn, settings, channel, want=5, session=Session())
    assert sum(1 for c in calls if c.startswith("https://x/")) == downloads  # known items are not fetched again


def test_claim_moves_track_and_lists_shorts_to_rerender(conn, settings, channel):
    p = str(tone(settings.music_dir / "space" / "x.mp3", seconds=3))
    music.sync(conn, settings)
    tid = conn.execute("SELECT id FROM tracks").fetchone()[0]
    waiting = short(conn, music_path=p, status="approved")
    short(conn, music_path=p, status="scheduled", youtube_id="abc")
    assert music.claim(conn, settings, tid) == [waiting]
    assert not music.usable(conn, "space")
    assert (settings.music_dir / music.CLAIMED / "x.mp3").exists()
    music.sync(conn, settings)  # the claimed folder is never scanned back in
    assert not music.usable(conn, "space")


def test_music_page(conn, settings, channel):
    tone(settings.music_dir / "space" / "Calm piano.mp3", seconds=3)
    app = create_app(settings, start_runner=False)
    client = app.test_client()
    page = client.get("/music?channel=space").data
    assert b"Calm piano" in page and b"Fetch music now" in page
    tid = conn.execute("SELECT id FROM tracks").fetchone()[0]
    client.post(f"/music/{tid}", data={"channel": "space", "action": "mood", "mood": "tense"})
    assert conn.execute("SELECT mood FROM tracks WHERE id=?", (tid,)).fetchone()[0] == "tense"
    assert client.get(f"/music/file/{tid}").status_code == 200
    r = client.post(f"/music/{tid}", data={"channel": "space", "action": "claimed"})
    assert r.status_code == 302
    assert conn.execute("SELECT command FROM jobs ORDER BY id DESC").fetchone()[0] == f'["music", "--claimed", "{tid}"]'


def test_mood_from_words():
    assert music.mood_from_words("Epic Cinematic Trailer") == "epic"
    assert music.mood_from_words("untitled 3") is None
