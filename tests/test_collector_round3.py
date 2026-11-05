from pathlib import Path

from meta.collector import round3


def _c(label, src, faults=(), old=0.0, score=0.5):
    return {"label": label, "source": src, "thumb_score": score, "gates": {"faults": list(faults), "old_look": old}}


def test_order_videos_before_photos():
    cands = [_c("a", "openverse", score=0.9), _c("b", "commons_video", score=0.2), _c("c", "library", score=0.4)]
    assert [c["label"] for c in round3.order(cands)] == ["c", "b", "a"]


def test_choose_skips_failed_check_and_bad_gates_and_prefers_old_for_history():
    made = [_c("a", "library"), _c("b", "commons_video", faults=["text on screen"]), _c("c", "commons_video", old=0.05)]
    assert round3.choose(made, {"b", "c"}, "space")["label"] == "c"
    assert round3.choose(made, {"a", "c"}, "space")["label"] == "a"
    assert round3.choose(made, {"a", "c"}, "history")["label"] == "c"
    assert round3.choose(made, set(), "space") is None


def test_collect_fetches_more_only_when_first_batch_fails(tmp_path, monkeypatch):
    cands = [dict(_c(str(i), "commons_video"), title=f"t{i}") for i in range(5)]
    for c in cands:
        c.pop("gates")
    def fake_cands(subject, lib, scorer, keys):
        assert lib is None or len(lib) == 2  # pieces/sweep expect (settings, conn)
        return [dict(c) for c in cands]
    monkeypatch.setattr(round3.pieces, "candidates", fake_cands)
    fetched = []

    def fake_fetch(c, subject, scorer, tmp):
        fetched.append(c["label"])
        p = tmp / f"{c['label']}.mp4"
        p.write_bytes(b"x")
        return p
    monkeypatch.setattr(round3.pieces, "fetch_video", fake_fetch)
    monkeypatch.setattr(round3.pieces, "clip_frames", lambda p, s: [])
    monkeypatch.setattr(round3.pieces, "strip", lambda f: b"")
    monkeypatch.setattr(round3.pieces, "duration", lambda p: 8.0)
    monkeypatch.setattr(round3, "probe_size", lambda p: (1280, 720))

    class G:
        def check(self, frames):
            return {"faults": [], "old_look": 0.0}
    sub = round3.subject("space", "Saturn", None)
    clip, rec = round3.collect(sub, 7.0, ("s", "c", "llm"), None, G(), {}, tmp_path, lambda items: {"K1"})
    assert fetched == ["K0", "K1", "K2"] and clip["path"].endswith("K1.mp4") and rec["result"] == "commons_video"
    fetched.clear()
    clip, rec = round3.collect(sub, 7.0, None, None, G(), {}, tmp_path, lambda items: {"K4"} if len(items) == 2 else set())
    assert fetched == ["K0", "K1", "K2", "K3", "K4"] and clip["path"].endswith("K4.mp4")
    clip, rec = round3.collect(sub, 7.0, None, None, G(), {}, tmp_path, lambda items: set())
    assert clip is None and rec["result"] == "nothing passed"


def test_runner_allows_round3():
    from meta import runner
    cmd = runner.command({"id": "2026-11-05-round3-1", "name": "round3", "n": 2})
    assert "meta.collector.round3" in cmd and cmd[-2:] == ["--part", "2"]


def _video(path: Path):
    import subprocess
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=640x360:r=30:d=8",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


class _Conn:
    def execute(self, *a):
        class R:
            def fetchall(self):
                return []
        return R()


class _Settings:
    claude_cmd = "x"


def test_new_ranking_and_quiz_render(tmp_path, monkeypatch):
    src = tmp_path / "src.mp4"
    _video(src)
    clip = {"id": -5, "path": str(src), "start": 0.0, "end": 8.0, "width": 640, "height": 360, "title": "t", "source": "library"}
    monkeypatch.setattr(round3, "collect", lambda s, *a: (dict(clip, id=-abs(hash(s["name"])) % 999 - 1), {"name": s["name"]}))

    class LLM:
        @staticmethod
        def ask_json(prompt, cmd, retries=1):
            if "ranking" in prompt:
                return {"title": "T", "heading": "Ranked by size",
                        "items": [{"rank": i, "name": f"N{i}", "value": f"{10 - i} m"} for i in range(1, 6)]}
            return {"title": "Q", "rounds": [{"answer": f"A{i}", "fact": "f"} for i in range(3)]}
    lib = (_Settings(), _Conn(), LLM)
    r = round3.new_ranking("space", lib, None, None, {}, tmp_path, None, tmp_path / "r.mp4")
    assert "error" not in r and (tmp_path / "r.mp4").stat().st_size > 10_000 and len(r["items"]) == 5
    q = round3.new_quiz("space", lib, None, None, {}, tmp_path, None, tmp_path / "q.mp4")
    assert "error" not in q and (tmp_path / "q.mp4").exists()


def test_main_runs_both_versions_with_letters(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("PIECES_NO_REEXEC", "1")
    monkeypatch.setattr(round3, "OUT", tmp_path)
    monkeypatch.setattr(round3.sweep, "open_library", lambda: (_Settings(), _Conn()))
    monkeypatch.setattr(round3.sweep, "load_keys", lambda s: {})
    monkeypatch.setattr(round3.sweep, "Scorer", lambda s: None)
    monkeypatch.setattr(round3.pieces, "Gates", lambda s: None)

    def fake_new(niche, lib, scorer, gates, keys, tmp, ask, out):
        assert len(lib) == 3
        out.write_bytes(b"x")
        return {"title": "new"}

    def fake_old(kind, niche, lib, out_dir):
        f = out_dir / f"{kind}.mp4"
        f.write_bytes(b"y")
        return {"title": "old", "_file": str(f)}
    monkeypatch.setattr(round3, "new_ranking", fake_new)
    monkeypatch.setattr(round3, "new_quiz", fake_new)
    monkeypatch.setattr(round3, "old_short", fake_old)
    round3.main(["--niche", "space"])
    d = json.loads((tmp_path / "made-part1.json").read_text())
    assert [r["kind"] for r in d["shorts"]] == ["ranking", "quiz"]
    for r in d["shorts"]:
        assert sorted(r["key"].values()) == ["new", "today"]
        assert (tmp_path / "shorts" / r["new"]["file"]).exists() and (tmp_path / "shorts" / r["today"]["file"]).exists()


def test_two_subjects_in_one_folder_keep_their_own_clips(tmp_path, monkeypatch):
    """Round 3 bug: pieces are named K0, K1... so a shared folder let subject 2 overwrite subject 1."""
    monkeypatch.setattr(round3.pieces, "candidates", lambda *a: [{"label": "x", "source": "commons_video", "title": "t"}])

    def fake_fetch(c, subject, scorer, tmp):
        p = tmp / f"{c['label']}.mp4"
        p.write_text(subject["name"])
        return p
    monkeypatch.setattr(round3.pieces, "fetch_video", fake_fetch)
    monkeypatch.setattr(round3.pieces, "clip_frames", lambda p, s: [])
    monkeypatch.setattr(round3.pieces, "strip", lambda f: b"")
    monkeypatch.setattr(round3.pieces, "duration", lambda p: 8.0)
    monkeypatch.setattr(round3, "probe_size", lambda p: (1280, 720))

    class G:
        def check(self, frames):
            return {"faults": [], "old_look": 0.0}
    a, _ = round3.collect(round3.subject("space", "Saturn", None), 7.0, None, None, G(), {}, tmp_path, lambda i: {"K0"})
    b, _ = round3.collect(round3.subject("space", "Jupiter", None), 7.0, None, None, G(), {}, tmp_path, lambda i: {"K0"})
    assert Path(a["path"]).read_text() == "Saturn" and Path(b["path"]).read_text() == "Jupiter"


def test_new_only_keeps_todays_version_and_letters(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("PIECES_NO_REEXEC", "1")
    monkeypatch.setattr(round3, "OUT", tmp_path)
    (tmp_path / "shorts").mkdir()
    (tmp_path / "shorts" / "space-ranking-T.mp4").write_bytes(b"old")
    (tmp_path / "made-part1.json").write_text(json.dumps({"shorts": [
        {"niche": "space", "kind": "ranking", "key": {"N": "new", "T": "today"}, "new": {"file": "space-ranking-N.mp4"},
         "today": {"title": "old", "file": "space-ranking-T.mp4"}}]}))
    monkeypatch.setattr(round3.sweep, "open_library", lambda: (_Settings(), _Conn()))
    monkeypatch.setattr(round3.sweep, "load_keys", lambda s: {})
    monkeypatch.setattr(round3.sweep, "Scorer", lambda s: None)
    monkeypatch.setattr(round3.pieces, "Gates", lambda s: None)

    def fake_new(niche, lib, scorer, gates, keys, tmp, ask, out):
        out.write_bytes(b"new")
        return {"title": "new"}
    monkeypatch.setattr(round3, "new_ranking", fake_new)
    monkeypatch.setattr(round3, "new_quiz", fake_new)
    monkeypatch.setattr(round3, "old_short", lambda *a: {"title": "today quiz"})
    round3.main(["--niche", "space", "--new-only"])
    d = json.loads((tmp_path / "made-part1.json").read_text())
    r = d["shorts"][0]
    assert r["key"] == {"N": "new", "T": "today"} and r["today"]["title"] == "old" and r["remade"] == 1
    assert (tmp_path / "shorts" / "space-ranking-N.mp4").read_bytes() == b"new"
    assert (tmp_path / "shorts" / "space-ranking-T.mp4").read_bytes() == b"old"
    assert d["shorts"][1]["today"]["title"] == "today quiz"  # no earlier quiz: made both ways


def test_runner_allows_round3_new_only():
    from meta import runner
    cmd = runner.command({"id": "2026-11-05-round3new-1", "name": "round3new", "n": 1})
    assert cmd[-3:] == ["--part", "1", "--new-only"] and runner.JSON_DIR["round3new"] == "round3"
