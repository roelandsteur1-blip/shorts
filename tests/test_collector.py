"""The production footage collector (shorts/collector.py), topic-first quiz/ranking/text card
(meta/topic.py), topic-first classic heroes and the detector crop (shorts/detect.py)."""
import dataclasses
import json
import subprocess

import pytest

from shorts import collector as col, detect, metatype, pipeline, render
from meta import topic
from meta.collector import pieces


def video(path, seconds=3, size="640x360"):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25",
                    "-t", str(seconds), "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


class Gates:
    def __init__(self, faults=None):
        self.faults = faults or {}

    def check(self, frames):
        return {"faults": [], "old_look": -0.05, "motion": 10.0, "brightness": 120.0, "text_share": 0.0}


def made(label, faults=(), old=None):
    return {"label": label, "gates": {"faults": list(faults), "old_look": old}}


def test_choose_rules():
    assert col.choose([made("A", ["black"]), made("B")], {"A", "B"}, "ocean")["label"] == "B"
    assert col.choose([made("A", ["black"])], {"A"}, "space")["label"] == "A"  # dark sky is fine in space
    assert col.choose([made("A", ["text on screen"])], {"A"}, "space") is None
    assert col.choose([made("A", ["frozen", "blurry"])], {"A"}, "ocean")["label"] == "A"  # not predictive
    assert col.choose([made("A", old=-0.01), made("B", old=0.05)], {"A", "B"}, "oldcity")["label"] == "B"
    assert col.choose([made("A", old=-0.01), made("B", old=0.05)], {"A", "B"}, "ocean")["label"] == "A"
    assert col.choose([made("A")], set(), "ocean") is None  # Claude said no


def test_rights():
    assert col.rights({"source": "openverse", "licence": "by 2.0", "creator": "Jan", "provider": "flickr"}) == \
        ("CC BY 2.0", "Jan / flickr (CC BY 2.0)")
    assert col.rights({"source": "commons_photo", "licence": "CC BY 4.0", "creator": "Ann"}) == \
        ("CC BY 4.0", "Ann / Wikimedia Commons (CC BY 4.0)")
    assert col.rights({"source": "openverse", "licence": "pdm 1.0"}) == ("public-domain", None)
    assert col.rights({"source": "openverse", "licence": "cc0 1.0"}) == ("CC0", None)
    assert col.rights({"source": "nasa", "licence": "public-domain"}) == ("public-domain", None)
    assert col.rights({"source": "commons_video", "licence": "public-domain"}) == ("public-domain", None)


def _collector(conn, settings, channel, verify, **ch):
    c = col.Collector(conn, settings, dataclasses.replace(channel, **ch), verify=verify,
                      ask_json=lambda prompt: {"picks": {"1": "none"}})
    c._tools = (None, Gates(), {})
    return c


def _library_scene(conn, path, title="Saturn V launch"):
    aid = conn.execute("INSERT INTO assets (source, source_id, title, license, path, width, height, status) "
                       "VALUES ('nasa', ?, ?, 'public-domain', ?, 640, 360, 'scened')", (title, title, str(path))).lastrowid
    return conn.execute("INSERT INTO scenes (asset_id, start, end) VALUES (?, 0, 3)", (aid,)).lastrowid


def test_library_first(conn, settings, channel, tmp_path, monkeypatch):
    v = video(tmp_path / "lib.mp4")
    sid = _library_scene(conn, v)
    import meta.textcard as tc
    monkeypatch.setattr(tc, "find_clips", lambda c, s, q, used, need, per_query=2: [
        {"id": sid, "path": str(v), "start": 0.0, "end": 3.0, "title": "Saturn V launch"}])
    monkeypatch.setattr(pieces, "candidates", lambda *a, **k: pytest.fail("archives searched although the library had it"))
    seen = []
    c = _collector(conn, settings, channel, lambda items: seen.extend(items) or {"L0"})
    assert c.find("Saturn V", need=2.0) == sid
    assert seen[0][0] == "L0" and seen[0][2] == "Saturn V"
    assert sid in c.used and c.log[0]["from"] == "library"


def test_archives_when_library_fails(conn, settings, channel, tmp_path, monkeypatch):
    import meta.textcard as tc
    from shorts import embed
    monkeypatch.setattr(tc, "find_clips", lambda *a, **k: [])
    monkeypatch.setattr(embed, "embed_pending", lambda *a, **k: 0)
    asked = {}

    def cands(subject, lib, scorer, keys, sources=None):
        asked["sources"] = sources
        return [{"source": "openverse", "kind": "photo", "title": "Saturn V photo", "licence": "by 2.0",
                 "creator": "Jan", "provider": "flickr", "ref": "https://example.org/p1", "file": "x", "thumb_score": 0.3},
                {"source": "commons_video", "title": "Saturn V film", "licence": "public-domain",
                 "ref": "https://commons/v1", "file": "y", "thumb_score": 0.1}]

    def fetch(c, tmp, seconds=5.0, **k):
        c.setdefault("fetch", {})["at"] = 1.0
        return video(tmp / f"{c['label']}.mp4", seconds=2)

    monkeypatch.setattr(pieces, "candidates", cands)
    monkeypatch.setattr(pieces, "fetch_photo", fetch)
    monkeypatch.setattr(pieces, "fetch_video", lambda c, s, sc, tmp, seconds=None: fetch(c, tmp))
    checked = []
    # video first: K0 is the Commons film; Claude only passes the photo (K1)
    c = _collector(conn, settings, channel, lambda items: checked.extend(i[0] for i in items) or {"K1"},
                   collector_sources=["Library today", "Commons video (name)", "Openverse photos"])
    sid = c.find("Saturn V", need=2.0)
    assert asked["sources"] == [("commons_video", 3), ("openverse", 2)]
    assert sorted(checked) == ["K0", "K1"]
    row = conn.execute("SELECT a.* FROM scenes sc JOIN assets a ON a.id = sc.asset_id WHERE sc.id=?", (sid,)).fetchone()
    assert (row["source"], row["license"], row["attribution"]) == ("openverse", "CC BY 2.0", "Jan / flickr (CC BY 2.0)")
    assert row["status"] == "scened" and row["path"].endswith(".mp4") and row["width"] == 640
    assert render.footage_credit(row) == "Jan / flickr (CC BY 2.0)"
    fit = conn.execute("SELECT fits, method FROM channel_fit WHERE asset_id=?", (row["id"],)).fetchone()
    assert tuple(fit) == (1, "collector")
    clip = col.clip(conn, sid)
    assert clip["path"] == row["path"] and clip["end"] == pytest.approx(2.0, abs=0.1)


def test_nothing_found(conn, settings, channel, monkeypatch):
    import meta.textcard as tc
    monkeypatch.setattr(tc, "find_clips", lambda *a, **k: [])
    monkeypatch.setattr(pieces, "candidates", lambda *a, **k: [])
    c = _collector(conn, settings, channel, lambda items: set())
    assert c.find("Nothing at all") is None and c.log[0]["scene"] is None


class FakeCollector:
    def __init__(self, conn, have):
        self.conn, self.have, self.asked = conn, have, []

    def find(self, name, show=None, need=8.0, hook=False):
        self.asked.append(name)
        return self.have.get(name)


class LLM:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def ask_json(self, prompt, cmd, retries=1, images=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def _lib(conn, settings, tmp_path, llm, found):
    v = video(tmp_path / "c.mp4", seconds=2)
    have = {name: _library_scene(conn, v, name) for name in found}
    return {"settings": settings, "conn": conn, "llm": llm, "out_dir": tmp_path / "out", "avoid": ["Old title"],
            "collector": FakeCollector(conn, have), "name": "Space", "handle": "@space"}


def test_ranking_writes_a_new_script_when_footage_is_short(conn, settings, tmp_path, monkeypatch):
    first = {"title": "t1", "heading": "h", "items": [{"rank": i, "name": f"A{i}", "value": f"{i} m"} for i in range(1, 8)]}
    second = {"title": "Ranking rockets", "heading": "Ranked by height",
              "items": [{"rank": i, "name": f"B{i}", "value": f"{10 - i} m"} for i in range(1, 8)]}
    llm = LLM([first, second])
    lib = _lib(conn, settings, tmp_path, llm, ["B2", "B3", "B4", "B6", "B7"])
    rendered = {}
    monkeypatch.setattr(topic, "render_segments", lambda segs, track, out: rendered.setdefault("segs", segs) and 18.0)
    recs = topic.ranking("space", lib)
    assert len(llm.prompts) == 2 and "A4" in llm.prompts[1] and "Old title" in llm.prompts[0]
    assert len(recs) == 1
    items = recs[0]["items"]
    assert [it["name"] for it in items] == ["B2", "B3", "B4", "B6", "B7"]
    assert [it["rank"] for it in items] == [1, 2, 3, 4, 5]
    assert recs[0]["heading"] == "Ranked by height" and len(recs[0]["scene_ids"]) == 5
    # the first script stopped as soon as too few subjects were left to make 4
    assert lib["collector"].asked[:5] == ["A1", "A2", "A3", "A4", "B1"]


def test_ranking_gives_up_after_three_scripts(conn, settings, tmp_path, monkeypatch):
    plan = {"title": "t", "items": [{"rank": 1, "name": "X", "value": "1"}]}
    llm = LLM([plan] * 3)
    lib = _lib(conn, settings, tmp_path, llm, [])
    assert topic.ranking("space", lib) == [] and len(llm.prompts) == topic.SCRIPTS
    assert "film era" not in llm.prompts[0]


def test_retrotech_subjects_from_the_film_era(conn, settings, tmp_path):
    for fn, reply in ((topic.ranking, {"items": []}), (topic.quiz, {"rounds": []}), (topic.textcard, {"posts": []})):
        llm = LLM([reply] * 3)
        fn("retrotech", _lib(conn, settings, tmp_path, llm, []))
        assert "film era, about 1890 to 1990" in llm.prompts[0]


def test_quiz_uses_first_three_found(conn, settings, tmp_path, monkeypatch):
    llm = LLM([{"title": "Name all 3", "rounds": [{"answer": n, "fact": "f", "show": "s"} for n in "PQRST"]}])
    lib = _lib(conn, settings, tmp_path, llm, ["P", "R", "S", "T"])
    monkeypatch.setattr(topic, "render_segments", lambda segs, track, out: 20.0)
    recs = topic.quiz("space", lib)
    assert [r["answer"] for r in recs[0]["rounds"]] == ["P", "R", "S"]


def test_textcard_topic_first(conn, settings, tmp_path, monkeypatch):
    posts = {"posts": [{"text": "Wrong post.", "subject": "Moon", "title": "a"},
                       {"text": "Saturn  could float in water.", "subject": "Saturn", "show": "rings", "title": "b",
                        "tags": ["saturn"], "highlight": ["float"]}]}
    llm = LLM([posts, {"checks": [{"post": 1, "ok": False}, {"post": 2, "ok": True, "fix": ""}]}])
    lib = _lib(conn, settings, tmp_path, llm, ["Saturn"])
    import meta.textcard as tc
    monkeypatch.setattr(tc, "render", lambda card, clip, track, secs, out: None)
    recs = topic.textcard("space", lib)
    assert recs[0]["text"] == "Saturn could float in water." and recs[0]["visual"] == "Saturn"
    assert lib["collector"].asked == ["Saturn"]  # the post that failed the fact check isn't searched


def test_meta_tries_topic_first_then_the_same_type(conn, settings, channel, monkeypatch):
    settings.collector = True
    ch = dataclasses.replace(channel, video_type="ranking")
    monkeypatch.setattr(topic, "ranking", lambda niche, lib: [])
    import meta.ranking as rk
    old = []
    monkeypatch.setattr(rk, "make", lambda niche, n, lib=None: old.append(lib["collector"]) or [])
    monkeypatch.setattr(col.Collector, "__init__", lambda self, *a, **k: None)
    assert metatype._one(conn, settings, ch, "space", "ranking") == [] and len(old) == 1


def test_classic_heroes_topic_first(conn, settings, channel, monkeypatch):
    settings.collector = True

    class C:
        def __init__(self):
            self.asked = []

        def _ask(self, prompt):
            self.prompt = prompt
            return {"subjects": [{"name": "Saturn V", "show": "launch"}, {"name": "Nothing"}, {"name": "Apollo 11"}]}

        def find(self, name, show=None, need=8.0, hook=False):
            self.asked.append((name, need))
            return {"Saturn V": 11, "Apollo 11": 12}.get(name)

    c = C()
    assert col.classic_heroes(conn, settings, channel, 2, collector=c) == [11, 12]
    assert c.asked[0] == ("Saturn V", col.HERO_SECONDS) and channel.name in c.prompt
    monkeypatch.setattr(col, "classic_heroes", lambda *a, **k: [7])
    assert pipeline.topic_heroes(conn, settings, channel, 3) == [7]
    settings.collector = False
    assert pipeline.topic_heroes(conn, settings, channel, 3) == []


def test_detector_path():
    fw = 0.32
    assert detect.path_from_boxes([(0, None), (1, ([0.1, 0, 0.2, 1], 0.05))], fw) is None  # weak box: noise
    p = detect.path_from_boxes([(0, ([0.1, 0.2, 0.3, 0.8], 0.5)), (1, ([0.5, 0.2, 0.7, 0.8], 0.4))], fw)
    assert p["x0"] == pytest.approx(0.2) and p["x1"] == pytest.approx(0.6) and not p["whole"]
    wide = detect.path_from_boxes([(0, ([0.05, 0.2, 0.95, 0.8], 0.6))], fw)
    assert wide["whole"] and wide["x0"] == wide["x1"] == pytest.approx(0.5)
    edge = detect.path_from_boxes([(0, ([0.0, 0.2, 0.1, 0.8], 0.6))], fw)
    assert edge["x0"] == pytest.approx(fw / 2)  # the window stays inside the frame


def test_detector_crop_in_render(tmp_path, monkeypatch):
    v = video(tmp_path / "b.mp4")
    blocks = [{"path": str(v), "src_start": 0.0, "seconds": 2.0, "avail": 3.0, "width": 640, "height": 360,
               "crop_x": 0.5, "visual": "hero"},
              {"path": str(v), "src_start": 0.0, "seconds": 2.0, "avail": 3.0, "width": 640, "height": 360,
               "crop_x": 0.5, "visual": "a red rocket"}]
    asked = []

    def window(path, start, secs, w, h, text, cache=None):
        asked.append(text)
        return {"x0": 0.3, "x1": 0.4, "whole": text == "Saturn V"}

    monkeypatch.setattr(detect, "subject_window", window)
    out = render._subject_paths(tmp_path, blocks, True, "Saturn V")
    assert asked == ["Saturn V", "a red rocket"]  # block 1's visual is "hero": the Short's subject
    assert out[0]["whole"] and (out[1]["x0"], out[1]["x1"], out[1]["whole"]) == (0.3, 0.4, False)
    asked.clear()
    render._subject_paths(tmp_path, blocks, True, None)  # detector crop off
    assert asked == []
    assert json.loads((tmp_path / "framing.json").read_text())
