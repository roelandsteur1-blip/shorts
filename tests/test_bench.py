"""The clip quality bench end to end on the synthetic library, with a fake model and a fake judge."""
import json

import numpy as np
import pytest

from shorts import bench, llm
from shorts.bench import clips, frames, interest, judge, metrics, models, rank, report, testset


class FakeModel:
    """Vectors from an image's average colour and from a text's hash: deterministic, no torch."""
    load_seconds = 0.0

    def __init__(self, name=None, threads=None):
        pass

    def arrays(self, images, pad=False):
        v = np.stack([np.concatenate([img.reshape(-1, 3).mean(axis=0) / 255, np.ones(5)]) for img in images])
        return (v / np.linalg.norm(v, axis=1, keepdims=True)).astype(np.float32)

    def texts(self, texts):
        out = []
        for t in texts:
            v = np.random.default_rng(abs(hash(t)) % 2**32).random(8) + 0.1
            out.append(v / np.linalg.norm(v))
        return np.array(out, np.float32)


@pytest.fixture
def shorts_made(library):
    conn = library
    scenes = [r[0] for r in conn.execute("SELECT id FROM scenes WHERE rejected = 0 ORDER BY id")]
    for n in range(3):
        cur = conn.execute("INSERT INTO shorts (channel, format_id, hero_scene_id, topic, status) VALUES (?,?,?,?,?)",
                           ("space", "fact", scenes[n], f"Saturn topic {n}", "rendered"))
        sid = cur.lastrowid
        for i, (text, visual) in enumerate([("Look at this", "hero"), ("Rings of ice", "Saturn rings close up"),
                                            ("A rocket at night", "rocket launch at night")]):
            conn.execute("INSERT INTO blocks (short_id, idx, text, seconds, visual, scene_id) VALUES (?,?,?,?,?,?)",
                         (sid, i, text, 2.0, visual, scenes[(n + i) % len(scenes)]))
    conn.commit()
    return conn


@pytest.fixture
def bench_run(settings, shorts_made, monkeypatch):
    monkeypatch.setattr(bench, "FOLDER", "bench-test")
    monkeypatch.setattr(models, "Encoder", FakeModel)
    monkeypatch.setattr(clips, "Encoder", FakeModel)
    monkeypatch.setattr(models, "MODELS", {"clip-b32": models.MODELS["clip-b32"],
                                           "siglip2-b16": models.MODELS["siglip2-b16"]})
    monkeypatch.setattr(clips, "MODELS", models.MODELS)
    monkeypatch.setattr(rank, "MODELS", models.MODELS)
    monkeypatch.setattr(interest, "MODELS", models.MODELS)
    monkeypatch.setattr(report, "MODELS", models.MODELS)
    return settings


def fake_judge(prompt, claude_cmd="claude", retries=1, conn=None, stage="claude", images=None):
    assert images, "the judge must see the clips"
    if "Image 1 shows the clip every half second" in prompt:
        n = int(prompt.split("numbered 1 to ")[1].split(" ")[0])
        return {"frames": {str(i + 1): (2 if i % 3 else 1) for i in range(n)}, "best_start": 2,
                "crops": {"A": 2, "B": 1, "C": 0, "D": 2}, "best_crop": "A"}
    return {str(i + 1): [i % 3, 1 + i % 5] for i in range(40)}


def test_bench_end_to_end(bench_run, monkeypatch):
    settings = bench_run
    monkeypatch.setattr(llm, "ask_json", fake_judge)
    ts = testset.build(settings, blocks=4, library=1000)
    assert ts["blocks"] and all(b["visual"] != "hero" for b in ts["blocks"])
    assert all(str(b["picked"]) in ts["scenes"] for b in ts["blocks"])  # the pipeline's own pick is searchable
    assert testset.build(settings) == ts  # fixed once made

    frames.extract(settings, ts, workers=1)
    sid = next(iter(ts["scenes"]))
    assert all(frames.frame_path(settings, sid, k).exists() for k in range(3))

    for name in models.MODELS:
        models.embed(settings, ts, name, threads=1)
        e = models.load_emb(settings, name)
        assert e["img"].shape[:2] == (len(ts["scenes"]), 3) and e["queries"].shape[0] == len(ts["blocks"])
        assert e["secs_per_image"] > 0

    ranks = rank.run(settings, ts)
    assert {"today", "pipeline-now", "clip-b32/max3+meta", "siglip2-b16/mean3"} <= set(ranks)
    key = str(ts["blocks"][0]["id"])
    assert len(ranks["today"][key]) == min(rank.TOP, len(ts["scenes"]))

    from shorts.bench.__main__ import pool_methods
    pools = rank.pool(ts, ranks, pool_methods(ranks), 10)
    assert ts["blocks"][0]["picked"] in pools[key] and len(pools[key]) <= 10
    labels = judge.run_topic(settings, ts, pools)
    assert set(labels[key]) == {str(s) for s in pools[key]}
    calls = []
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: calls.append(1) or {})
    judge.run_topic(settings, ts, pools)
    assert not calls  # all labelled: nothing asked twice
    monkeypatch.setattr(llm, "ask_json", fake_judge)

    # make sure there are good long clips to test seconds and crops on
    for per_block in labels.values():
        for s in per_block:
            per_block[s] = [2, 4]
    data = clips.measure(settings, ts, labels, n=3, threads=1, finders=[])
    d = next(iter(data.values()))
    assert {"start", "middle", "motion", "match:clip-b32", "match+motion:siglip2-b16"} <= set(d["w"])
    assert {"centre", "saliency", "today", "guide:siglip2-b16"} <= set(d["s"])
    assert all(0 <= c["x"] <= 1 for c in d["s"].values())
    data = clips.run_judge(settings, ts)
    assert all("judge" in d for d in data.values())

    res = interest.run(settings, ts, labels, threads=1, aesthetic=False)
    assert "sharpness" in res["scores"] and "prompts:siglip2-b16" in res["scores"]

    text = report.build(settings)
    for heading in ("On topic", "The right seconds", "The right crop", "Visually interesting"):
        assert heading in text
    assert "| today |" in text

    # A person checks the judge on the dashboard, one clip at a time; the report compares.
    from shorts.web import create_app
    client = create_app(settings, start_runner=False).test_client()
    assert b"Check the judge" in client.get("/bench").data
    page = client.get("/bench/check")
    assert page.status_code == 200 and b"/bench/strip/" in page.data
    item = json.loads((bench.root(settings) / "human-sample.json").read_text())[0]
    assert client.get(f"/bench/strip/{item['sid']}.jpg").data[:2] == b"\xff\xd8"
    r = client.post("/bench/check", data={"block": item["block"], "sid": item["sid"], "topic": "2", "watch": "1"})
    assert r.status_code == 302
    assert "agree with Roeland" in report.build(settings)


def test_judge_survives_the_usage_limit(bench_run, monkeypatch):
    settings = bench_run
    ts = testset.build(settings, blocks=4, library=1000)
    frames.extract(settings, ts, workers=1)

    def limit(*a, **k):
        raise llm.LLMLimitError("limit")
    monkeypatch.setattr(llm, "ask_json", limit)
    pools = {str(b["id"]): [int(s) for s in list(ts["scenes"])[:3]] for b in ts["blocks"]}
    assert judge.run_topic(settings, ts, pools) == {}


def test_metrics():
    labels = {"1": {"10": [2, 3], "11": [0, 1], "12": [1, 2]}, "2": {"20": [0, 1], "21": [2, 5]}}
    s = metrics.topic_scores({"1": [[10, 0.9], [11, 0.5]], "2": [[20, 0.9], [21, 0.8]]}, labels)
    assert s["n"] == 2 and s["p1"] == 0.5 and s["hit5"] == 1.0
    assert metrics.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert metrics.auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert metrics.auc([0.1], [0.9]) == 0.0
    w = metrics.window_scores([0, 0.5, 1, 1.5, 2], 0.5, 1.0, {"2": 2, "3": 1}, 1)
    assert w["in_view"] == pytest.approx(0.75) and w["off_by"] == 0.5 and w["all_clear"] == 0.0


def test_crop_letters_merge_same_crops():
    letters = clips._letters({"a": {"x": 0.5, "whole": False}, "b": {"x": 0.51, "whole": False},
                              "c": {"x": 0.2, "whole": False}, "d": {"x": 0.5, "whole": True},
                              "e": {"x": 0.5, "whole": True}})
    groups = sorted(sorted(v) for v in letters.values())
    assert groups == [["a", "b"], ["c"], ["d", "e"]]
    assert json.dumps(letters)  # stored in clips.json


def test_narration_reads_audio_without_pyav(bench_run, shorts_made, tmp_path, monkeypatch):
    """faster-whisper gets the samples as an array (a file path would go through PyAV), and one
    video that fails doesn't stop the others."""
    import sys
    import types

    from shorts.bench import asr

    conn = shorts_made
    for n, (aid,) in enumerate(conn.execute("SELECT id FROM assets ORDER BY id").fetchall()):
        conn.execute("UPDATE assets SET file_url=? WHERE id=?", (str(tmp_path / f"raw{n}" / "raw.mp4"), aid))
    conn.commit()
    calls = []

    class Model:
        def __init__(self, *a, **kw):
            pass

        def transcribe(self, audio, **kw):
            assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
            calls.append(len(audio))
            if len(calls) == 2:
                raise RuntimeError("broken audio")
            seg = types.SimpleNamespace(start=0.0, end=30.0, text="the rings of Saturn")
            return iter([seg]), types.SimpleNamespace(duration=len(audio) / asr.RATE, language="en")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    ts = testset.build(bench_run, 4, 50)
    texts = asr.run(bench_run, ts)
    meta = bench.load(bench.root(bench_run) / "asr-meta.json")
    assert len(calls) == 2 and abs(calls[0] / asr.RATE - 24) < 1  # the 24 s test film, 16 kHz
    assert sorted(m.get("error", "ok")[:12] for m in meta.values()) == ["RuntimeError", "ok"]
    assert texts and set(texts.values()) == {"the rings of Saturn"}
