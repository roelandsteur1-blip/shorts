"""On-topic footage and subject crop: the script writer sees a contact sheet and picks clips
(shorts/menu.py, script.pick_clips), the fallback search never takes a poor match from another
video (shots.MIN_MATCH), and the crop follows the block's words (framing.text_guide)."""
import json

import cv2
import numpy as np

from shorts import embed, framing, hero, llm, menu, script, shots


def test_image_message_and_stream_result():
    msg = json.loads(llm.image_message("hi", [b"\xff\xd8jpeg"]))
    content = msg["message"]["content"]
    assert msg["type"] == "user" and content[0] == {"type": "text", "text": "hi"}
    assert content[1]["type"] == "image" and content[1]["source"]["media_type"] == "image/jpeg"
    out = "\n".join([json.dumps({"type": "system"}), "not json",
                     json.dumps({"type": "result", "result": "[1]", "is_error": False})])
    assert llm.result_envelope(out)["result"] == "[1]"
    assert llm.result_envelope("nothing") is None


def test_script_sees_contact_sheet_and_picks_clips(library, settings, channel, monkeypatch):
    heroes = hero.candidates(library, settings, channel.id, 1)
    seen = {}

    def fake(prompt, claude_cmd, conn, stage, images=None):
        seen["prompt"], seen["images"] = prompt, images
        return json.dumps([{"hero": 1, "topic": "t", "blocks": [
            {"text": "You are looking at Saturn now", "visual": "hero"},
            {"text": "Its rings are mostly ice", "visual": "rings", "clip": "C1"},
            {"text": "Stay on this view", "visual": "hero", "clip": "hero"},
            {"text": "The same clip twice", "visual": "rings again", "clip": "C1"},
            {"text": "A clip that is not there", "visual": "moon", "clip": "C99"}]}])
    monkeypatch.setattr(llm, "_ask", fake)
    sid = script.write_scripts(library, settings, channel, None, heroes)[0]

    assert seen["images"] and seen["images"][0][:2] == b"\xff\xd8"  # one JPEG contact sheet per hero
    img = cv2.imdecode(np.frombuffer(seen["images"][0], np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[1] == menu.TILE_W * menu.COLS
    assert "Clip menu (image 1): C1 " in seen["prompt"] and '"clip": "..."' in seen["prompt"]
    assert "HERO frames" in seen["prompt"]

    blocks = library.execute("SELECT visual, scene_id FROM blocks WHERE short_id=? ORDER BY idx", (sid,)).fetchall()
    hero_scene = library.execute("SELECT hero_scene_id FROM shorts WHERE id=?", (sid,)).fetchone()[0]
    c1 = blocks[1]["scene_id"]
    assert c1 and c1 != hero_scene
    # C1 is the closest scene of the hero's own video (a 24 s video gives one Short: all of it is its).
    assert library.execute("SELECT asset_id FROM scenes WHERE id=?", (c1,)).fetchone()[0] == \
        library.execute("SELECT asset_id FROM scenes WHERE id=?", (hero_scene,)).fetchone()[0]
    assert blocks[2]["visual"] == "hero" and blocks[2]["scene_id"] is None
    assert blocks[3]["scene_id"] is None and blocks[4]["scene_id"] is None  # left to the search


def test_menus_in_one_batch_never_share_a_clip(library, settings, channel):
    heroes = hero.candidates(library, settings, channel.id, 2)
    assert len(heroes) == 2
    taken: set[int] = set()
    a = menu.build(library, settings, channel, heroes[0], taken)
    b = menu.build(library, settings, channel, heroes[1], taken)
    assert a["clips"] and b["clips"] and a["image"] and b["image"]
    assert not set(a["clips"].values()) & set(b["clips"].values())
    assert heroes[0] not in a["clips"].values()


def test_pick_clips_skips_a_clip_another_short_took(library, settings, channel):
    heroes = hero.candidates(library, settings, channel.id, 1)
    h = script.hero_context(library, heroes[0])
    h["menu"] = menu.build(library, settings, channel, heroes[0], set())
    first = h["menu"]["clips"]["C1"]
    library.execute("INSERT INTO shorts (id, channel, format_id, status) VALUES (999, 'other', 'f', 'rendered')")
    library.execute("INSERT INTO blocks (short_id, idx, text, seconds, scene_id) VALUES (999, 1, 'x', 2, ?)", (first,))
    out = script.pick_clips(library, h, [{"text": "a"}, {"text": "b", "clip": "c1"}, {"text": "c", "clip": "C2"}])
    assert "scene_id" not in out[1]
    assert out[2]["scene_id"] == h["menu"]["clips"]["C2"]


def _scene(conn, asset_id, start, vec):
    return conn.execute("INSERT INTO scenes (asset_id, start, end, embedding) VALUES (?, ?, ?, ?)",
                        (asset_id, start, start + 6, vec.astype(np.float32).tobytes())).lastrowid


def test_fallback_search_stays_in_hero_video_rather_than_a_poor_match(conn, settings):
    enc = embed.get_encoder(settings)
    q = enc.texts(["fighter jet taking off, jets"])[0]
    other = np.random.default_rng(1).standard_normal(q.size).astype(np.float32)
    other -= (other @ q) * q
    other /= np.linalg.norm(other)

    def mix(sim):  # a unit vector with this similarity to the query
        return sim * q + np.sqrt(1 - sim * sim) * other

    hero_video = conn.execute("INSERT INTO assets (source, source_id, title, duration, status, license) "
                              "VALUES ('nasa', 'a', 'jets', 60, 'scened', 'public-domain')").lastrowid
    elsewhere = conn.execute("INSERT INTO assets (source, source_id, title, duration, status, license) "
                             "VALUES ('nasa', 'b', 'misc', 60, 'scened', 'public-domain')").lastrowid
    hero_scene = _scene(conn, hero_video, 0, mix(0.3))
    own = _scene(conn, hero_video, 12, mix(0.05))
    poor = _scene(conn, elsewhere, 0, mix(0.15))
    short = conn.execute("INSERT INTO shorts (channel, format_id, hero_scene_id, topic, status) "
                         "VALUES ('space', 'f', ?, 'jets', 'scripted')", (hero_scene,)).lastrowid
    conn.executemany("INSERT INTO blocks (short_id, idx, text, seconds, visual, scene_id) VALUES (?,?,?,?,?,?)",
                     [(short, 0, "hook", 5.5, "hero", hero_scene), (short, 1, "b", 2.5, "fighter jet taking off", None)])
    conn.commit()
    shots.assign(conn, settings, short)
    assert conn.execute("SELECT scene_id FROM blocks WHERE short_id=? AND idx=1", (short,)).fetchone()[0] == own

    # A clearly matching scene from another video does win.
    good = _scene(conn, elsewhere, 20, mix(0.6))
    conn.execute("UPDATE blocks SET scene_id=NULL WHERE short_id=? AND idx=1", (short,))
    conn.commit()
    shots.assign(conn, settings, short)
    assert conn.execute("SELECT scene_id FROM blocks WHERE short_id=? AND idx=1", (short,)).fetchone()[0] == good
    assert poor not in {r[0] for r in conn.execute("SELECT scene_id FROM blocks")}


class ColourEncoder:
    """Images: how much of them is pure red. Text: 'red'. So the similarity is the red share."""
    dim = 2

    def _v(self, r):
        v = np.array([r, 1 - r], dtype=np.float32) + 1e-6
        return v / np.linalg.norm(v)

    def arrays(self, images):
        return np.stack([self._v(float(np.mean((i[:, :, 2] > 200) & (i[:, :, 1] < 50)))) for i in images])

    def texts(self, texts):
        return np.stack([np.array([1, 0], dtype=np.float32) for _ in texts])


def test_crop_aims_at_the_words_not_at_busy_texture():
    rng = np.random.default_rng(0)
    frames = []
    for _ in range(4):
        f = np.full((216, 384, 3), 90, np.uint8)
        f[:, 260:] = rng.integers(0, 255, (216, 124, 3), dtype=np.uint8)  # busy noise on the right
        f[80:140, 40:90] = (0, 0, 255)                                     # the red subject on the left
        frames.append(f)
    plain = framing.analyse(frames, 1920, 1080)
    guide = framing.text_guide(frames, framing.window_fraction(1920, 1080)[0], "red", ColourEncoder())
    aimed = framing.analyse(frames, 1920, 1080, guide)
    assert guide["centre"] < 0.35 and guide["spread"] > framing.GUIDE_SPREAD
    assert aimed["x0"] < 0.35 < plain["x0"]
    assert not aimed["whole"]


def test_whole_frame_when_the_subject_is_wider_than_the_window():
    frames = [np.full((216, 384, 3), 90, np.uint8) for _ in range(3)]
    guide = {"centre": 0.5, "spread": 0.03, "best": 0.24, "whole": 0.29}
    assert framing.analyse(frames, 1920, 1080, guide)["whole"]
    assert not framing.analyse(frames, 1920, 1080, {**guide, "whole": 0.25})["whole"]
    assert framing.text_guide(frames, 1.0, "red", ColourEncoder()) is None  # nothing to aim sideways


def test_render_aims_each_block_at_its_visual_or_the_topic():
    from shorts.render import block_subject
    assert block_subject({"visual": "fighter jet"}, "Jets") == "fighter jet"
    assert block_subject({"visual": "hero"}, "Jets") == "Jets"
    assert block_subject({"visual": None}, None) is None


def test_subject_path_reads_real_frames(tmp_path, monkeypatch):
    """The crop must come from the video itself: a failing frame read used to be swallowed and
    every render quietly fell back to the old stored crop centre."""
    from conftest import make_video
    video = tmp_path / "v.mp4"
    make_video(video, seconds=6)
    assert len(framing.read_frames(str(video), 1.0, 2.0, 1280, 720)) >= 3
    warnings = []
    monkeypatch.setattr(framing.log, "warning", lambda *a: warnings.append(a))
    res = framing.subject_path(str(video), 1.0, 2.0, 1280, 720, fallback_x=0.123,
                               query="red", encoder=ColourEncoder())
    assert not warnings
    assert res["motion"] is not None and res["x0"] != 0.123
