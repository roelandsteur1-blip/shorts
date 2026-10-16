import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from shorts import db, embed, llm
from shorts.config import Settings, load_channel


class FakeEncoder:
    """Deterministic stand-in for CLIP: hashes words/paths into vectors."""
    dim = 32

    def _vec(self, key: str) -> np.ndarray:
        seed = int(hashlib.md5(key.encode()).hexdigest()[:8], 16)
        v = np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)
        return v / np.linalg.norm(v)

    def images(self, paths):
        return np.stack([self._vec(str(Path(p).parent.name) + Path(p).name) for p in paths])

    def texts(self, texts):
        return np.stack([self._vec(t) for t in texts])


@pytest.fixture
def settings(tmp_path):
    # restock_below=0: tests never download footage unless they ask for it.
    s = Settings(data_dir=tmp_path / "data", encoder="libx264", restock_below=0)
    s.ensure_dirs()
    return s


@pytest.fixture
def conn(settings):
    c = db.connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def channel():
    return load_channel("space")


@pytest.fixture(autouse=True)
def fake_encoder():
    embed.set_encoder(FakeEncoder())
    yield
    embed.set_encoder(None)


def make_video(path: Path, seconds: int = 24) -> None:
    """Synthetic 16:9 video with a hard cut every 6 seconds and an audio track (must be stripped)."""
    parts = []
    srcs = ["testsrc2", "mandelbrot", "life=s=320x180:mold=10:r=30:ratio=0.5", "rgbtestsrc"]
    for i in range(seconds // 6):
        p = path.parent / f"part{i}.mp4"
        src = srcs[i % len(srcs)]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"{src}" + ("" if "=" in src else "=s=1280x720:r=30"),
                        "-f", "lavfi", "-i", "sine=f=440", "-t", "6", "-vf", "scale=1280:720,setsar=1",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
        parts.append(p)
    lst = path.parent / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", str(path)], check=True)


@pytest.fixture
def library(conn, settings, tmp_path):
    """Two ingested 'NASA' videos, audio stripped, scened and embedded."""
    from shorts import media, scenes
    from shorts.sources.nasa import upsert_metadata

    for n, title in enumerate(["Saturn rings flyby", "Rocket launch at night"]):
        raw = tmp_path / f"raw{n}" / "raw.mp4"
        raw.parent.mkdir()
        make_video(raw)
        aid = upsert_metadata(conn, {"nasa_id": f"TEST-{n}", "title": title,
                                     "description": f"{title}. Cassini saw the rings in 2004.",
                                     "keywords": ["saturn", "launch"], "center": "JPL"})
        dst = settings.footage_dir / "nasa" / f"TEST-{n}.mp4"
        media.strip_audio(raw, dst)
        info = media.probe(dst)
        conn.execute("UPDATE assets SET path=?, duration=?, width=?, height=?, status='downloaded' WHERE id=?",
                     (str(dst), info["duration"], info["width"], info["height"], aid))
    conn.commit()
    scenes.process_all(conn, settings)
    embed.embed_pending(conn, settings)
    from shorts.categorize import sort_pending
    sort_pending(conn, settings)
    return conn


def fake_claude(monkeypatch):
    """Answer script and fact-check prompts like Claude would, without calling it."""
    def ask(prompt, claude_cmd="claude", timeout=600):
        if "fact checker" in prompt:
            import re
            ids = [int(x) for x in re.findall(r"^Short (\d+):", prompt, re.M)]
            return json.dumps([{"short": i, "facts": [
                {"claim": "Cassini reached Saturn in 2004", "verdict": "supported", "note": ""},
                {"claim": "Saturn's rings are mostly water ice", "verdict": "unclear", "note": "not in source"},
            ]} for i in ids])
        n = prompt.count("\nHero ")
        return "```json\n" + json.dumps([{
            "hero": i + 1, "topic": f"topic {i}",
            "blocks": [{"text": "You're looking at *Saturn* right now", "visual": "hero"},
                       {"text": "Those rings are wider than eight Earths side by side", "visual": "Saturn rings close up"},
                       {"text": "Yet most are thinner than a building", "visual": "rocket launch at night"},
                       {"text": "Cassini reached it in *2004*", "visual": "spacecraft"},
                       {"text": "And spent 13 years watching", "visual": "planet"}],
            "facts": [{"claim": "Cassini reached Saturn in 2004", "source": "nasa", "quote": "Cassini saw the rings in 2004."},
                      {"claim": "Saturn's rings are mostly water ice", "source": "https://science.nasa.gov/saturn/", "quote": "..."}],
            "title": "Saturn's rings are stranger than you think", "description": "A closer look.",
            "tags": ["saturn", "#space"]} for i in range(n)]) + "\n```"
    monkeypatch.setattr(llm, "ask", ask)
