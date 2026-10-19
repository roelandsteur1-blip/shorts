"""Voice-over: block lengths follow the speech, voice + ducked music in the render."""
import numpy as np

from conftest import fake_claude
from shorts import media, pipeline, voice


class FakeVoice:
    """Half a second of tone per word."""
    def __init__(self):
        self.calls = []

    def speak(self, text, v, speed=1.0):
        self.calls.append((text, v))
        n = int(voice.SR * 0.5 * len(text.split()) / speed)
        return (0.3 * np.sin(np.arange(n) * 2 * np.pi * 220 / voice.SR)).astype(np.float32)


def test_voiced_channel_short_has_speech_lengths_and_audio(library, settings, channel, monkeypatch):
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=330:d=40",
                    str(settings.music_dir / "track.mp3")], check=True)
    fake = FakeVoice()
    voice.set_engine(fake)
    channel.voice = "bm_george"
    fake_claude(monkeypatch)
    done = pipeline.make(library, settings, channel, count=1)
    assert len(done) == 1
    s = library.execute("SELECT * FROM shorts WHERE id=?", (done[0],)).fetchone()
    assert s["voice"] == "bm_george" and s["music"]
    blocks = library.execute("SELECT text, seconds FROM blocks WHERE short_id=? ORDER BY idx", (done[0],)).fetchall()
    assert fake.calls and all("*" not in t for t, _ in fake.calls)
    # The blocks together last at least as long as the speech, every block at least MIN_BLOCK.
    assert sum(b["seconds"] for b in blocks) >= 0.5 * sum(len(b["text"].split()) for b in blocks) - 0.01
    assert all(b["seconds"] >= voice.MIN_BLOCK - 0.01 for b in blocks)
    info = media.probe(s["video_path"])
    assert info["has_audio"]
    assert abs(info["duration"] - sum(b["seconds"] for b in blocks)) < 0.5

    # Same text again: no new speech. Edited text: spoken again.
    n = len(fake.calls)
    voice.prepare(library, settings, channel, done[0])
    assert len(fake.calls) == n


def test_text_only_channel_and_missing_engine(conn, settings, channel):
    conn.execute("INSERT INTO shorts (id, channel, format_id, status) VALUES (1, 'space', 'f', 'checked')")
    channel.voice = None
    assert voice.assign(conn, channel, 1) is None
    channel.voice = "af_heart"
    assert voice.assign(conn, channel, 1) == "af_heart"
    conn.execute("INSERT INTO blocks (short_id, idx, text, seconds) VALUES (1, 0, 'Hello *world*', 2)")
    conn.commit()
    voice.set_engine(False)  # Kokoro not installed: the Short quietly stays text-only
    assert voice.prepare(conn, settings, channel, 1) is None
    assert conn.execute("SELECT voice FROM shorts WHERE id=1").fetchone()[0] is None


def test_voices_page_samples_and_saving(settings, conn, tmp_path):
    import shutil
    from pathlib import Path
    from shorts.config import load_channel
    from shorts.web import create_app
    settings.config_root = tmp_path / "config"  # saving writes channel files: not the repo's
    shutil.copytree(Path(__file__).parents[1] / "config", settings.config_root,
                    ignore=shutil.ignore_patterns("local.toml", "*.bak"))
    client = create_app(settings, start_runner=False).test_client()
    page = client.get("/voices").data
    assert b"Michael, American man" in page and b"not installed" in page
    assert client.get("/voices/sample/am_michael").status_code == 503

    fake = FakeVoice()
    voice.set_engine(fake)
    r = client.get("/voices/sample/bf_emma?speed=1.1")
    assert r.status_code == 200 and r.data[:4] == b"RIFF"
    client.get("/voices/sample/bf_emma?speed=1.1")
    assert len(fake.calls) == 1  # made once, then served from disk
    assert client.get("/voices/sample/nobody").status_code == 404

    r = client.post("/voices/space", data={"voice": "bm_george", "voice_speed": "1.05"})
    assert r.status_code == 302
    ch = load_channel("space", settings.config_dir)
    assert (ch.voice, ch.voice_speed) == ("bm_george", 1.05)
    assert b"listen to the voices" in client.get("/channels/space").data
    client.post("/voices/space", data={"voice": "", "voice_speed": "1.0"})
    assert load_channel("space", settings.config_dir).voice is None
    assert client.post("/voices/space", data={"voice": "x", "voice_speed": "1"}).status_code == 400


class TimedVoice(FakeVoice):
    """Half a second per word, with word timings like Kokoro's."""
    def speak_words(self, text, v, speed=1.0):
        audio = self.speak(text, v, speed)
        words = [(w, i * 0.5 + 0.05, i * 0.5 + 0.45) for i, w in enumerate(text.split())]
        return audio, words


def test_whole_sentences_are_spoken_in_one_go(conn, settings, channel):
    conn.execute("INSERT INTO shorts (id, channel, format_id, status, voice) VALUES (1, 'space', 'f', 'checked', 'am_michael')")
    texts = ["The Moon has *no wind*", "so the footprints stay.", "Forever?", "Probably for a million years"]
    for i, t in enumerate(texts):
        conn.execute("INSERT INTO blocks (short_id, idx, text, seconds) VALUES (1, ?, ?, 2)", (i, t))
    conn.commit()
    channel.min_seconds = 0
    assert voice.sentences([{"text": t} for t in texts]) == [[0, 1], [2], [3]]
    fake = TimedVoice()
    voice.set_engine(fake)
    voice.prepare(conn, settings, channel, 1)
    # One call per sentence, not per block.
    assert [t for t, _ in fake.calls] == ["The Moon has no wind so the footprints stay.", "Forever?",
                                          "Probably for a million years"]
    secs = [r[0] for r in conn.execute("SELECT seconds FROM blocks WHERE short_id=1 ORDER BY idx")]
    # The first block ends halfway between "wind" (ends 2.45) and "so" (starts 2.55): no pause.
    assert abs(secs[0] - 2.5) < 0.01
    # The sentence end gets the pause: the other 2.0 s of the sentence's 4.5 s, plus GAP.
    assert abs(secs[1] - (2.0 + voice.GAP)) < 0.01


def test_block_cuts_fall_back_to_letters():
    cuts = voice.block_cuts(["aaaa bbbb", "cccc"], 3.0, [("aaaa", 0, 1)])  # timings don't match the words
    assert cuts == [2.0]
