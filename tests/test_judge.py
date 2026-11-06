"""Footage judges (shorts/judge.py), the collector using them, the judge test job
(meta/collector/judgetest.py) and its side-by-side page."""
import base64
import dataclasses
import json
import subprocess

import pytest

from shorts import collector as col, judge, llm
from meta.collector import judgetest, pieces


def video(path, seconds=3, size="640x360"):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25",
                    "-t", str(seconds), "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def v(shows=True, conf=5, action=3, subject=3, wow=3, looks=3, same=True, era=True, why="x", viral=3):
    return judge._clean({"shows": shows, "confidence": conf, "action": action, "subject": subject, "wow": wow,
                         "looks": looks, "viral": viral, "same_shot": same, "era_ok": era, "why": why})


def test_interest_and_passes():
    assert judge.interest({"action": 5, "subject": 5, "wow": 5, "viral": 5, "looks": 5}) == 5.0
    assert judge.interest({"action": 1, "subject": 1, "wow": 1, "viral": 1, "looks": 1}) == 1.0
    assert judge.interest({}) == 1.0
    assert v(action=5, subject=3, wow=3, looks=1)["interest"] == pytest.approx(0.3 * 5 + 0.2 * 3 * 3 + 0.1)
    assert judge.passes(v()) and not judge.passes(v(conf=3)) and not judge.passes(v(shows=False))
    assert not judge.passes(v(same=False)) and not judge.passes(v(era=False)) and not judge.passes(None)
    clean = judge._clean({"shows": "yes", "confidence": 9, "action": "x"})
    assert clean["shows"] is False and clean["confidence"] == 5 and clean["action"] == 1


def test_combine():
    g, c = v(action=5, wow=4), v(action=2, wow=2, conf=2, shows=False)
    out = judge.combine(g, c)
    assert out["shows"] is True  # Claude's unsure no doesn't veto Gemini
    assert out["action"] == 4.0 and out["wow"] == 3.0
    assert judge.combine(g, v(shows=False, conf=5))["shows"] is False  # a confident no does
    assert judge.combine(None, c) == c and judge.combine(None, None) is None


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


def test_gemini_judge(monkeypatch, tmp_path):
    clip = video(tmp_path / "a.mp4", seconds=2)
    sent = {}
    monkeypatch.setattr(judge, "_models", {})
    monkeypatch.setattr(judge.requests, "get", lambda url, params, timeout: Resp(200, {"models": [
        {"name": "models/gemini-3.8-flash-preview-10-2026", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3-flash", "supportedGenerationMethods": ["generateContent"]}]}))
    calls = []

    def post(url, headers, json, timeout):
        calls.append(url)
        if len(calls) == 1:
            return Resp(429, {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0s"}]}})
        sent.update(json)
        answer = {"clips": [{"label": "A", "shows": True, "confidence": 5, "why": "rings", "same_shot": True,
                             "era_ok": True, "action": 4, "subject": 5, "wow": 4, "viral": 4, "looks": 4}, {"label": "Z", "shows": True}]}
        return Resp(200, {"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(answer)}]}}]})

    monkeypatch.setattr(judge.requests, "post", post)
    monkeypatch.setattr(judge.time, "sleep", lambda s: None)
    out = judge.gemini_judge("KEY", [{"label": "A", "path": str(clip), "seconds": 2.0}], "Saturn", "space")
    assert set(out) == {"A"} and out["A"]["interest"] > 3 and judge.passes(out["A"])
    assert calls[-1].endswith("models/gemini-3.8-flash-preview-10-2026:generateContent")
    parts = sent["contents"][0]["parts"]
    assert "Saturn" in parts[0]["text"] and "always true" in parts[0]["text"]
    assert base64.b64decode(parts[2]["inline_data"]["data"])[4:8] == b"ftyp"
    with pytest.raises(judge.JudgeError):
        judge.gemini_model("KEY", "gemini-9")


def test_claude_judge_sends_frames(monkeypatch, settings, tmp_path):
    clip = video(tmp_path / "a.mp4", seconds=3)
    got = {}

    def fake(prompt, cmd, retries=1, images=None, model=""):
        got.update(prompt=prompt, images=images, model=model)
        return {"clips": [{"label": "A", "shows": True, "confidence": 4, "action": 2}]}

    monkeypatch.setattr(llm, "ask_json", fake)
    out = judge.claude_judge(settings, [{"label": "A", "path": str(clip), "seconds": 3.0}], "Hindenburg",
                             "history", model="opus")
    assert len(got["images"]) == judge.FRAMES and got["model"] == "opus"
    assert "old film" in got["prompt"] and "clip A = images 1-5" in got["prompt"]
    assert out["A"]["shows"] and out["A"]["action"] == 2


def test_mode_falls_back_without_key(settings):
    s = dataclasses.replace(settings, footage_judge="both")
    assert judge.mode(s) == "claude+"
    from shorts import sources
    sources.set_api_key(s, "gemini", "k")
    assert judge.mode(s) == "both"


def test_collector_picks_most_interesting(conn, settings, channel, tmp_path, monkeypatch):
    import meta.textcard as tc
    paths = [video(tmp_path / f"l{i}.mp4") for i in range(3)]
    ids = []
    for i, p in enumerate(paths):
        aid = conn.execute("INSERT INTO assets (source, source_id, title, license, path, width, height, status) "
                           "VALUES ('nasa', ?, 'x', 'public-domain', ?, 640, 360, 'scened')", (str(i), str(p))).lastrowid
        ids.append(conn.execute("INSERT INTO scenes (asset_id, start, end) VALUES (?, 0, 3)", (aid,)).lastrowid)
    monkeypatch.setattr(tc, "find_clips", lambda *a, **k: [
        {"id": sid, "path": str(p), "start": 0.0, "end": 3.0} for sid, p in zip(ids, paths)])
    monkeypatch.setattr(pieces, "candidates", lambda *a, **k: [])

    class Gates:
        def check(self, frames):
            return {"faults": [], "old_look": -0.05}

    seen = {}

    def fake_judge(clips, name, niche, show):
        seen["clips"] = clips
        return {"L0": v(wow=2, action=2), "L1": v(wow=5, action=5), "L2": v(shows=False, wow=5, action=5)}

    c = col.Collector(conn, settings, channel, judge=fake_judge)
    c._tools = (None, Gates(), {})
    assert c.find("Saturn V", need=2.0) == ids[1]
    assert seen["clips"][0]["path"] == str(paths[0]) and seen["clips"][0]["seconds"] == 2.0
    assert c.log[0]["interest"] == 4.0  # action 5, wow 5, subject/viral/looks 3
    # every clip dull -> none from the library
    c2 = col.Collector(conn, settings, channel, judge=lambda *a: {"L0": v(1, 5, 1, 1, 1, 1)})
    c2._tools = (None, Gates(), {})
    c2.used = set()
    monkeypatch.setattr(tc, "find_clips", lambda *a, **k: [{"id": ids[0], "path": str(paths[0]), "start": 0.0, "end": 3.0}])
    assert c2.find("Saturn V", need=2.0) is None


def test_collector_today_check_unchanged(conn, settings, channel, monkeypatch):
    # footage_judge "claude" (the default) still uses the old yes/no verify
    assert settings.footage_judge == "claude"
    c = col.Collector(conn, settings, channel, verify=lambda items: {"A"})
    assert c.check([{"label": "A", "strip": b"x", "path": "p"}], {"name": "x"}, 8.0) == {"A"}


# ---------- the test job ----------

def _round2(tmp_path):
    pieces_dir = tmp_path / "pieces"
    (pieces_dir / "landscape").mkdir(parents=True)
    (pieces_dir / "clips").mkdir()
    cands = []
    for lab, yes in (("A", True), ("B", True), ("C", False)):
        video(pieces_dir / "landscape" / f"space-saturn-{lab}.mkv", seconds=2)
        (pieces_dir / "clips" / f"space-saturn-{lab}.mp4").write_bytes(b"x")
        cands.append({"label": lab, "preview": f"space-saturn-{lab}.mp4", "claude_shows": yes, "source": "nasa",
                      "gates": {"faults": [], "old_look": 0}})
    (pieces_dir / "made-part1.json").write_text(json.dumps({"subjects": [
        {"niche": "space", "name": "Saturn", "candidates": cands},
        {"niche": "space", "name": "Unmarked", "candidates": cands}]}))
    marks = tmp_path / "marks.json"
    marks.write_text(json.dumps({"clips": {"space/Saturn": ["A"]}}))
    return pieces_dir, marks


def test_judgetest_metrics_and_pairs(tmp_path, monkeypatch):
    pieces_dir, marks = _round2(tmp_path)
    rows = judgetest.load_subjects(pieces_dir, marks)
    assert [r["key"] for r in rows] == ["space/Saturn"] and [c["bad"] for c in rows[0]["clips"]] == [True, False, False]
    s = rows[0]
    assert judgetest.old_pick(s) == "A"  # today: first yes in order, and Roeland marked it bad
    s["verdicts"] = {"today": {}, "both (g)": {"A": v(), "B": v(wow=5, action=5), "C": v(shows=False)}}
    s["picks"] = {"today": "A", "both (g)": judgetest.new_pick(s, s["verdicts"]["both (g)"])}
    assert s["picks"]["both (g)"] == "B"
    m = judgetest.metrics(rows, "both (g)")
    assert m["picks_bad"] == 0 and m["yes_good"] == 50 and m["agree"] == 33
    assert judgetest.metrics(rows, "today")["picks_bad"] == 1
    monkeypatch.setattr(judgetest, "HERE", tmp_path / "out")
    (tmp_path / "out").mkdir()
    judgetest.save(rows, ["g"], [], final=True)
    res = json.loads((tmp_path / "out" / "made.json").read_text())
    p = res["pairs"][0]
    assert res["new"] == "both (g)" and set(p["versions"].values()) == {"today", "new"}
    assert {f for _, f in p["files"]} == {"space-saturn-A.mp4", "space-saturn-B.mp4"}
    assert "Footage judge test" in (tmp_path / "out" / "report.md").read_text()


def test_judge_page(tmp_path, monkeypatch):
    from shorts.web import footage_review as fr
    monkeypatch.setattr(fr, "OUT", tmp_path)
    (tmp_path / "pieces" / "clips").mkdir(parents=True)
    for n in ("space-saturn-A.mp4", "space-saturn-B.mp4"):
        (tmp_path / "pieces" / "clips" / n).write_bytes(b"x")
    (tmp_path / "judgetest").mkdir()
    (tmp_path / "judgetest" / "made.json").write_text(json.dumps({"pairs": [
        {"key": "space/Saturn", "niche": "space", "name": "Saturn",
         "files": [["A", "space-saturn-A.mp4"], ["B", "space-saturn-B.mp4"]],
         "versions": {"A": "today", "B": "new"}, "new_interest": 3.9, "new_why": "rings visible"}]}))
    from flask import Flask
    app = Flask(__name__, template_folder=str(__import__("pathlib").Path(fr.__file__).parent.parent / "templates"))
    app.register_blueprint(fr.bp)
    c = app.test_client()
    r = c.get("/bench/footage/judge/0")
    assert r.status_code == 200 and b"space-saturn-B.mp4" in r.data
    assert b"New filter" in r.data and b"Today's filter" in r.data and b"rings visible" in r.data
    assert c.post("/bench/footage/judge/0", data={"best": "B", "boring": ["A", "Q"]}).status_code == 302
    assert c.post("/bench/footage/judge/0", data={"best": "Q"}).status_code == 400
    m = json.loads((tmp_path / "marks" / "footage.json").read_text())
    assert m["judge"]["space/Saturn"] == {"best": "B", "boring": ["A"], "wrong": [], "note": ""}
    assert c.get("/bench/footage/judge/1").status_code == 302
