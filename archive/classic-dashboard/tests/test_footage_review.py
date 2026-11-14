import json

from shorts.web import footage_review as fr


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "OUT", tmp_path)
    (tmp_path / "pieces" / "clips").mkdir(parents=True)
    (tmp_path / "crops" / "review").mkdir(parents=True)
    for n in ("space-saturn-A.mp4", "space-saturn-K.mp4"):
        (tmp_path / "pieces" / "clips" / n).write_bytes(b"x")
    (tmp_path / "pieces" / "made-part1.json").write_text(json.dumps({"subjects": [{"niche": "space", "name": "Saturn", "candidates": [
        {"preview": "space-saturn-K.mp4"}, {"preview": "space-saturn-A.mp4"}, {"preview": None}]}]}))
    (tmp_path / "crops" / "review" / "crop-space-saturn-A.mp4").write_bytes(b"x")
    (tmp_path / "crops" / "made.json").write_text(json.dumps({"clips": [
        {"review": "crop-space-saturn-A.mp4", "niche": "space", "name": "Saturn", "key": {"B": "today", "A": "whole"}}]}))
    (tmp_path / "crops" / "made-part1.json").write_text(json.dumps({"clips": [
        {"review": "crop-space-saturn-A.mp4", "niche": "space", "name": "Saturn", "key": {"C": "today", "D": "whole"}}]}))



def test_lists_and_newer_part_wins(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    s = fr.subjects()
    assert [c["letter"] for c in s[0]["clips"]] == ["A", "K"]
    assert fr.grids()[0]["letters"] == ["C", "D"]


def test_pages_save_marks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from flask import Flask
    app = Flask(__name__, template_folder=str(__import__("pathlib").Path(fr.__file__).parent.parent / "templates"))
    app.register_blueprint(fr.bp)
    app.jinja_env.filters["pct"] = lambda a, b: 0
    c = app.test_client()
    assert c.post("/bench/footage/clips/0", data={"bad": ["K", "Z"]}).status_code == 302
    assert c.post("/bench/footage/crops/0", data={"best": ["D", "C"], "cut": ["C"]}).status_code == 302
    assert c.post("/bench/footage/crops/0", data={"best": "Q"}).status_code == 400
    assert c.post("/bench/footage/crops/0", data={}).status_code == 400
    assert c.get("/bench/footage/crops/0").status_code == 200
    m = json.loads((tmp_path / "marks" / "footage.json").read_text())
    assert m["clips"] == {"space/Saturn": ["K"]}
    assert m["crops"] == {"crop-space-saturn-A.mp4": {"best": ["C", "D"], "cut": ["C"]}}
    assert c.get("/bench/footage/grid/..%2Fx.mp4").status_code == 404
    assert c.get("/bench/footage/clip/space-saturn-A.mp4").status_code == 200


def test_round3_pairs_and_marks(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "OUT", tmp_path)
    (tmp_path / "round3" / "shorts").mkdir(parents=True)
    for n in ("space-quiz-B.mp4", "space-quiz-Q.mp4"):
        (tmp_path / "round3" / "shorts" / n).write_bytes(b"x")
    (tmp_path / "round3" / "made-part1.json").write_text(json.dumps({"shorts": [
        {"niche": "space", "kind": "quiz", "key": {"Q": "new", "B": "today"},
         "new": {"file": "space-quiz-Q.mp4"}, "today": {"file": "space-quiz-B.mp4"}},
        {"niche": "space", "kind": "ranking", "key": {"A": "new", "C": "today"}, "new": {"error": "x"}, "today": {}}]}))
    assert fr.pairs() == [{"key": "space-quiz", "niche": "space", "kind": "quiz",
                           "files": [("B", "space-quiz-B.mp4"), ("Q", "space-quiz-Q.mp4")]}]
    (tmp_path / "round3" / "shorts" / "space-ranking-C.mp4").write_bytes(b"x")
    (tmp_path / "round3" / "made-part1.json").write_text(json.dumps({"shorts": [
        {"niche": "space", "kind": "quiz", "key": {"Q": "new", "B": "today"},
         "new": {"file": "space-quiz-Q.mp4"}, "today": {"file": "space-quiz-B.mp4"}},
        {"niche": "space", "kind": "ranking", "key": {"A": "new", "C": "today"}, "new": {"error": "x"},
         "today": {"file": "space-ranking-C.mp4"}}]}))
    assert fr.pairs()[1]["files"] == [("C", "space-ranking-C.mp4")]
    from flask import Flask
    app = Flask(__name__, template_folder=str(__import__("pathlib").Path(fr.__file__).parent.parent / "templates"))
    app.register_blueprint(fr.bp)
    c = app.test_client()
    assert c.post("/bench/footage/shorts/0", data={"best": "Q", "wrong": ["B"], "note": "hi"}).status_code == 302
    assert c.post("/bench/footage/shorts/0", data={"best": "Z"}).status_code == 400
    assert b"Good enough" in c.get("/bench/footage/shorts/1").data
    assert c.post("/bench/footage/shorts/1", data={"best": "bad"}).status_code == 302
    m = json.loads((tmp_path / "marks" / "footage.json").read_text())
    assert m["shorts"]["space-quiz"] == {"best": "Q", "wrong": ["B"], "note": "hi"}
    assert m["shorts"]["space-ranking"]["best"] == "bad"


def test_remade_pair_gets_a_new_key(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "OUT", tmp_path)
    (tmp_path / "round3" / "shorts").mkdir(parents=True)
    (tmp_path / "round3" / "shorts" / "space-quiz-Q.mp4").write_bytes(b"x")
    (tmp_path / "round3" / "made-part1.json").write_text(json.dumps({"shorts": [
        {"niche": "space", "kind": "quiz", "remade": 1, "key": {"Q": "new", "B": "today"}, "new": {"file": "space-quiz-Q.mp4"}}]}))
    assert fr.pairs()[0]["key"] == "space-quiz-v2"
