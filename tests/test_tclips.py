"""Transcript-first maker (meta/transcript.py, meta/tclips.py): the pure helpers and one real
render. No network, no Claude, no Whisper."""
import subprocess

import numpy as np

from meta import runner, tclips
from meta import transcript as tx

SRT = """1
00:00:01,000 --> 00:00:03,000
Ladies and <i>gentlemen</i>,

2
00:00:03,000 --> 00:00:05,500
the car of tomorrow!

"""
VTT = """WEBVTT

NOTE made by hand

00:01.000 --> 00:02.500 align:start position:10%
Hello there.

1:02:03.250 --> 1:02:04.000
- <c.yellow>Late</c> line &amp; more
"""


def test_parse_srt_and_vtt():
    s = tx.parse_subs(SRT)
    assert [x["text"] for x in s] == ["Ladies and gentlemen ,", "the car of tomorrow!"] or \
        [x["text"] for x in s] == ["Ladies and gentlemen,", "the car of tomorrow!"]
    assert s[0]["start"] == 1.0 and s[1]["end"] == 5.5
    v = tx.parse_subs(VTT)
    assert [x["text"] for x in v] == ["Hello there.", "Late line & more"]
    assert v[1]["start"] == 3723.25


def test_spread_words_cover_the_line():
    w = tx.spread_words({"start": 10.0, "end": 12.0, "text": "one three fivefive"})
    assert [x[0] for x in w] == ["one", "three", "fivefive"]
    assert w[0][1] == 10.0 and abs(w[-1][2] - 12.0) < 0.02
    assert all(a[2] <= b[1] + 0.01 for a, b in zip(w, w[1:]))


def test_clean_drops_loops_and_fillers_over_silence():
    segs = [{"start": i, "end": i + 1, "text": t} for i, t in enumerate(
        ["He went in.", "It's not him.", "It's not him.", "It's not him.", "It's not him.", "Thank you.", "Thank you."])]
    quiet = lambda a, b: -60.0 if a >= 5 else -20.0  # noqa: E731
    kept, flags = tx.clean(segs, quiet)
    assert [s["text"] for s in kept] == ["He went in.", "It's not him.", "It's not him."]
    assert flags["repeats"] == 2
    # the same "thank you" over real speech stays
    kept, _ = tx.clean(segs[-1:], lambda a, b: -20.0)
    assert len(kept) == 1


def test_sentences_from_words_and_from_captions():
    whisper = [{"start": 0, "end": 4, "text": "Here it is. The car", "words": [
        ["Here", 0.0, 0.3], ["it", 0.3, 0.5], ["is.", 0.5, 0.9], ["The", 1.2, 1.4], ["car", 1.4, 1.8]]},
        {"start": 4, "end": 6, "text": "of tomorrow!", "words": [["of", 4.0, 4.2], ["tomorrow!", 4.2, 5.0]]}]
    s = tx.sentences(whisper)
    assert [x["text"] for x in s] == ["Here it is.", "The car of tomorrow!"]
    assert s[1]["start"] == 1.2 and s[1]["end"] == 5.0 and [x["i"] for x in s] == [0, 1]
    # captions without punctuation still give borders at line ends once long enough
    caps = [{"start": i * 6.0, "end": i * 6.0 + 5.5, "text": "and then the man walks on"} for i in range(8)]
    s = tx.sentences(caps, max_seconds=10)
    assert len(s) >= 3 and all(x["end"] - x["start"] <= 12 for x in s)
    assert "[0] 0:00" in tx.numbered(s)


def sents(n, each=4.0):
    return [{"i": i, "start": i * each, "end": i * each + each - 0.5, "text": f"Line {i}.",
             "words": [["Line", i * each, i * each + 1], [f"{i}.", i * each + 1, i * each + each - 0.5]]}
            for i in range(n)]


def test_snap_on_word_borders_with_air_and_lengths():
    s = sents(30)
    t = tclips.snap(s, 5, 9)  # sentences 5-9: 20.0 to 39.5 s, plus air
    assert t["start"] == round(20 - tclips.AIR, 2) and t["end"] == round(39.5 + tclips.AIR, 2)
    short = tclips.snap(s, 5, 5)  # too short: grows to >= 15 s
    assert short["end"] - short["start"] >= tclips.MIN_LEN and short["from"] == 5
    long = tclips.snap(s, 0, 29)  # too long: trimmed from the end to <= 45 s
    assert long["from"] == 0 and long["end"] - long["start"] <= tclips.MAX_LEN
    # one sentence longer than the 58 s cap can't be used
    huge = [{"i": 0, "start": 0.0, "end": 70.0, "text": "x", "words": []}]
    assert tclips.snap(huge, 0, 0) is None
    assert tclips.snap(sents(2), 0, 1) is None  # the whole source is shorter than 15 s


def test_candidates_drop_overlaps_and_unsafe_moments():
    s = sents(40)
    s[20]["text"] = "Two men were killed in the crash."
    moments = [{"from": 2, "to": 6, "scores": {"surprising": 5, "ad_safe": 5}},
               {"from": 4, "to": 8, "scores": {"surprising": 4}},             # overlaps the first
               {"from": 18, "to": 22, "scores": {"surprising": 5}},           # "killed"
               {"from": 30, "to": 34, "scores": {"surprising": 5, "ad_safe": 2}},
               {"from": "x"}]
    c = tclips.candidates(moments, s)
    assert [x["from"] for x in c] == [2]
    assert c[0]["claude"] == 5.0


def test_signal_score_and_loudness():
    base = {"claude": 4.0}
    assert tclips.signal_score({**base, "signals": {"blank_start": True}}) is None
    assert tclips.signal_score({**base, "signals": {"face_start": True, "peak_db": 12}}) == 4.5
    assert tclips.signal_score({**base, "signals": {"cuts_per_10s": 6}}) == 3.7
    rate = 8000
    quiet = (np.random.default_rng(1).normal(0, 300, rate * 4)).astype(np.int16)
    loud = quiet.copy()
    loud[rate * 2: rate * 2 + rate // 2] = (np.random.default_rng(2).normal(0, 9000, rate // 2)).astype(np.int16)
    assert tclips.loud_peak_db(loud, rate) > 15 > tclips.loud_peak_db(quiet, rate)


def test_layer_needs_two_real_elements():
    full = {"hook": "h", "context": "In 1957 ...", "payoff": "It was banned in 1962.",
            "layer": ["context", "payoff_fact", "nonsense"]}
    assert tclips.layer_ok(full) == ["context", "payoff_fact"]
    # claiming context without writing one doesn't count
    assert tclips.layer_ok({**full, "context": ""}) == ["payoff_fact"]
    assert len(tclips.layer_ok({"hook": "h", "payoff": "p", "layer": ["verdict"]})) < 2


def test_rights_rules_and_allowlist(tmp_path):
    f = tmp_path / "t.toml"
    f.write_text('[[title]]\nid = "my_film_1950"\nkind = "feature"\nrights = "not renewed"\n'
                 '[[title]]\nid = "bad"\nkind = "nonsense"\n[deny]\nwords = ["cbs", "restored"]\n')
    titles, deny = tclips.load_titles(f)
    assert [t["id"] for t in titles] == ["my_film_1950"] and deny == ["cbs", "restored"]
    ok = lambda kind, meta, ident="x", allowed=(): tclips.rights_ok(kind, meta, deny, set(allowed), ident)[0]  # noqa: E731
    assert ok("newsreel", {"title": "Universal Newsreel 1957 Release 12"})
    assert not ok("newsreel", {"title": "Some news film"})
    assert not ok("newsreel", {"title": "Universal Newsreel", "description": "a restored copy"})
    assert ok("prelinger", {"collection": ["prelinger"], "licenseurl": "http://creativecommons.org/licenses/publicdomain/"})
    assert not ok("prelinger", {"collection": ["prelinger"], "licenseurl": ""})
    assert ok("nasa", {"title": "Apollo 11 press conference", "center": "JSC"})
    assert not ok("nasa", {"title": "Launch", "description": "Courtesy of SpaceX"})
    assert not ok("feature", {"title": "Any film"})
    assert ok("feature", {"title": "Any film"}, "my_film_1950", {"my_film_1950"})
    # the shipped list loads and has a deny list
    assert "cbs" in tclips.load_titles()[1]


def test_subtitle_blocks_are_short_and_in_output_time():
    words = [["Ladies", 10.0, 10.4], ["and", 10.4, 10.5], ["gentlemen,", 10.5, 11.0], ["the", 11.1, 11.2],
             ["car", 11.2, 11.5], ["of", 11.5, 11.6], ["tomorrow!", 11.6, 12.2], ["It", 14.0, 14.2],
             ["flies.", 14.2, 14.8], ["Nope", 30.0, 30.5]]
    b = tclips.subtitle_blocks(words, 9.7, 15.1)
    assert [x[2] for x in b] == ["Ladies and gentlemen,", "the car of tomorrow!", "It flies."]
    assert b[0][0] == 0.3 and b[0][1] <= b[1][0] + 1e-6 and b[-1][1] <= 15.1 - 9.7
    assert all(len(x[2].split()) <= 6 for x in b)


def test_runner_allows_tclips_jobs():
    assert runner.command({"id": "2026-11-12-tclipsprep-1", "name": "tclipsprep", "n": 1})[-4:] == \
        ["meta.tclips", "prep", "--part", "1"]
    assert runner.command({"id": "2026-11-12-tclips-3", "name": "tclips", "n": 3})[-1] == "3"
    assert runner.command({"id": "2026-11-12-tclips-9", "name": "tclips", "n": 9}) is None
    assert "tclips" in runner.JSON_JOBS and runner.JSON_DIR["tclipsprep"] == "tclips"
    assert set(tclips.PARTS.values()) == set(tclips.KINDS)


def test_render_with_subtitles_and_bare(tmp_path):
    src = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=640x480:r=24:d=22",
                    "-f", "lavfi", "-i", "sine=d=22", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    words = [[f"word{i}{'.' if i % 5 == 4 else ''}", 1.0 + i * 0.5, 1.4 + i * 0.5] for i in range(36)]
    plan = {"hook": "They really sold this to doctors", "context": "1957: TV ads made medical claims freely",
            "payoff": "Two years later the claim was banned.", "credit": "Test film (1957)", "start": 1.0, "end": 19.0}
    for bare in (False, True):
        out = tmp_path / f"o-{bare}.mp4"
        total = tclips.render(str(src), plan, words, out, bare=bare)
        assert abs(total - 18.0) < 0.01  # FX off (Roeland): no payoff freeze
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True)
        assert "video,1080,1920" in p.stdout and "audio" in p.stdout


def test_make_source_end_to_end_with_a_fake_claude(tmp_path, monkeypatch):
    """The whole make step on a local test video: transcript (faked) -> moments -> signals ->
    frame pass -> layer -> review -> render, with Claude replaced by canned answers."""
    src = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=640x480:r=24:d=70",
                    "-f", "lavfi", "-i", "sine=d=70", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    monkeypatch.setattr(tclips, "OUT", tmp_path / "out")
    monkeypatch.setattr(tx, "get", lambda s: {"origin": "whisper", "sentences": sents(17)})

    class FakeLLM:
        prompts = []

        def ask_json(self, prompt, cmd, retries=1, images=None):
            self.prompts.append(prompt)
            if "candidate moments" in prompt:
                return {"moments": [{"from": 1, "to": 4, "why": "w", "scores": {"surprising": 5, "ad_safe": 5},
                                     "angle": {"context": "c"}},
                                    {"from": 8, "to": 11, "why": "w2", "scores": {"surprising": 3}}]}
            if "rank the best" in prompt:
                return {"ranked": ["M1", "M2"], "checks": [{"id": "M1", "ok": True}, {"id": "M2", "ok": True}]}
            if "OUR layer" in prompt:
                return {"usable": True, "hook": "They really did this", "context": "In 1957, on TV",
                        "payoff": "It was banned two years later.", "title": "t", "credit": "Test (1957)",
                        "layer": ["context", "payoff_fact"]}
            return {"ok": True, "notes": "fine"}

    class Settings:
        claude_cmd = "claude"
    llm = FakeLLM()
    made = tclips.make_source({"id": "test", "url": str(src), "title": "Test film", "year": 1957},
                              "newsreel", 0, Settings(), llm, per_source=2, bare_first=True)
    assert [m["short"] for m in made] == ["newsreel-1a", "newsreel-1b"]
    assert set(made[0]["lengths"]) == {"newsreel-1a.mp4", "newsreel-1a-bare.mp4"}
    assert made[0]["layer"] == ["context", "payoff_fact"] and made[0]["sentences"] == [1, 4]
    assert all(tclips.MIN_LEN <= v <= tclips.MAX_LEN + 1 for m in made for v in m["lengths"].values())
    assert (tmp_path / "out" / "newsreel-1b.mp4").exists()
    assert not list((tmp_path / "out" / "work").glob("*.mp4"))  # pieces cleaned up
    assert len(llm.prompts) == 2 + 2 * 2  # moments, rank, then layer + review per Short (FX off: no focus)


def test_checked_films_by_search_pick_one_clean_copy(monkeypatch):
    """A film on the list by search: the server picks one copy of the right year that isn't
    restored or colorized; it never falls back to the open searches for features."""
    titles = [{"title": "Reefer Madness", "search": "q-reefer", "year": 1936, "kind": "feature",
               "rights": "not renewed"}]
    docs = {"q-reefer": [{"identifier": "reefer_color", "year": "1936"}, {"identifier": "reefer_2005", "year": "2005"},
                         {"identifier": "reefer_ok", "year": "1936"}, {"identifier": "reefer_ok2", "year": "1936"}]}
    metas = {"reefer_color": {"title": "Reefer Madness (colorized)"}, "reefer_ok": {"title": "Reefer Madness"},
             "reefer_ok2": {"title": "Reefer Madness"}}
    monkeypatch.setattr(tclips, "search", lambda q, rows=30: docs.get(q, []))
    monkeypatch.setattr(tclips, "archive_item", lambda i: {"url": f"u/{i}", "length": 4000, "meta": metas[i],
                                                           "captions_url": None})
    got = tclips.find_archive("feature", 3, titles, ["colorized"])
    assert [g["id"] for g in got] == ["reefer_ok"]
    assert got[0]["rights"].startswith("film on the hand-checked list") and "not renewed" in got[0]["rights"]
    # the shipped list has the three checked films
    shipped = [t for t in tclips.load_titles()[0] if t["kind"] == "feature"]
    assert {t["title"] for t in shipped} == {"His Girl Friday", "Reefer Madness", "The Little Shop of Horrors"}


def test_signals_and_made_json_hold_plain_types(tmp_path):
    """tclips-1..3 (2026-11-12) crashed writing made.json: blank() gave a numpy bool."""
    import json
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:r=24:d=6",
                    "-f", "lavfi", "-i", "sine=d=6", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    sig = tclips.signals(str(src), 1.0, 4.0)
    json.dumps(sig)
    assert type(sig["blank_start"]) is bool and type(tclips.blank(None)) is bool
    tclips.save_json(tmp_path / "m.json", [{"x": np.bool_(True), "y": np.float32(1.5)}])
    assert json.loads((tmp_path / "m.json").read_text()) == [{"x": True, "y": 1.5}]


def test_share_copies_every_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(tclips, "OUT", tmp_path)
    for n in ("newsreel-1a.mp4", "feature-2a-bare.mp4"):
        (tmp_path / n).write_bytes(b"x")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "piece.mp4").write_bytes(b"x")
    got = tclips.share()
    assert sorted(f.name for f in got) == ["feature-2a-bare.mp4", "newsreel-1a.mp4"]
    assert runner.command({"id": "2026-11-12-tclipsshare-1", "name": "tclipsshare"})[-2:] == ["meta.tclips", "share"]


def test_drifting_audio_is_stretched_onto_the_film():
    """Reefer Madness (2026-11-12): 3541 s of WAV for 3600 s of film, so the subtitles ran ahead."""
    segs = [{"start": 100.0, "end": 102.0, "text": "a b", "words": [["a", 100.0, 101.0], ["b", 101.0, 102.0]]}]
    k = 3600 / 3541
    got = tx.stretch(segs, k)
    assert got[0]["start"] == round(100 * k, 3) and got[0]["words"][1][2] == round(102 * k, 3)
    src = {"id": "x", "length": 4098}
    assert tx.stale({"origin": "whisper", "audio_s": 3541}, src)
    assert not tx.stale({"origin": "whisper", "audio_s": 3600}, src)
    assert not tx.stale({"origin": "whisper", "audio_s": 3541, "timing": {"stretch": 1.0}}, src)
    assert not tx.stale({"origin": "nasa_captions"}, src)


def test_every_look_renders_one_subtitle_track(tmp_path):
    words = [[w, 1.0 + i * 0.4, 1.35 + i * 0.4] for i, w in enumerate("the car of tomorrow flies. Ladies and gentlemen".split())]
    for name, look in tclips.LOOKS.items():
        d = tmp_path / name
        d.mkdir()
        lst, h = tclips.subtitle_track(words, 0.5, 6.0, look, d)
        lines = lst.read_text().splitlines()
        total = sum(float(x.split()[1]) for x in lines if x.startswith("duration"))
        assert abs(total - 5.5) < 0.01 and lines[-1].startswith("file") and h > 0
        shots = [x for x in lines if x.startswith("file") and "blank" not in x]
        assert len(shots) == (8 if look.get("hot") else len(tclips.subtitle_blocks(words, 0.5, 6.0, look.get("max_words", 6))))
    assert tclips.LOOK in tclips.LOOKS


def test_look_test_list_and_job():
    import tomllib
    rows = tomllib.loads(tclips.LOOK_TEST.read_text())["short"]
    assert len(rows) == 5 and all(r.get("hook") and r.get("context") and r.get("payoff") for r in rows if r["name"] != "feature-1a")
    assert len({r["name"].split("-")[0] for r in rows}) >= 4  # different kinds of source
    assert runner.command({"id": "2026-11-13-tclipslooks-1", "name": "tclipslooks"})[-2:] == ["meta.tclips", "looks"]
    assert "RAGEBAIT" in tclips.LAYER_PROMPT


def test_looks_step_recuts_a_listed_short_in_every_look(tmp_path, monkeypatch):
    import json
    src = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:r=24:d=30",
                    "-f", "lavfi", "-i", "sine=d=30", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    monkeypatch.setattr(tclips, "OUT", tmp_path / "out")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "made.json").write_text(json.dumps([{"short": "nasa-3b", "source": "s1", "sentences": [1, 4]}]))
    (tmp_path / "out" / "sources-nasa.json").write_text(json.dumps([{"id": "s1", "url": str(src)}]))
    monkeypatch.setattr(tx, "get", lambda s: {"sentences": sents(7)})
    got = tclips.looks(["nasa-3b"])
    assert sorted(f.name for f in got) == sorted(f"nasa-3b-{k}.mp4" for k in tclips.TEST_THEMES)
    assert not list((tmp_path / "out" / "work").glob("*.mp4"))


def test_themes_style_every_text_and_point_at_real_parts():
    assert tclips.THEME in tclips.THEMES and len(tclips.TEST_THEMES) == 5
    for name, th in tclips.THEMES.items():
        assert th["subs"] in tclips.LOOKS, name
        for part in ("hook", "context", "payoff"):
            im = tclips.styled_block("We reached the Moon in 7 years. Then stopped", th[part])
            assert im.width == tclips.W and im.getbbox(), (name, part)


def test_long_silences_are_cut_out():
    """The X-15 Short (2026-11-13) had a long silence: pauses over MAX_GAP shrink to KEEP_GAP."""
    words = [["One", 1.0, 1.4], ["two.", 1.5, 2.0], ["Three", 9.0, 9.4], ["four.", 9.5, 10.0]]
    segs, out = tclips.tighten(words, 0.7, 10.3)
    assert len(segs) == 2 and segs[0][0] == 0.7 and segs[1][1] == 10.3
    total = sum(y - x for x, y in segs)
    assert abs(total - (10.3 - 0.7 - (7.0 - tclips.KEEP_GAP))) < 0.01
    assert out[2][1] == round(2.0 + tclips.KEEP_GAP - 0.7, 3) and out[-1][2] <= total + 1e-6
    # normal speech stays one stretch
    segs, out = tclips.tighten([[f"w{i}", 1.0 + i * 0.5, 1.4 + i * 0.5] for i in range(10)], 0.7, 5.9)
    assert segs == [(0.7, 5.9)] and out[0][1] == 0.3


def test_render_cuts_the_silence(tmp_path):
    src = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:r=24:d=30",
                    "-f", "lavfi", "-i", "sine=d=30", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    words = [[f"w{i}", 1.0 + i * 0.5, 1.4 + i * 0.5] for i in range(12)] + \
            [[f"v{i}", 20.0 + i * 0.5, 20.4 + i * 0.5] for i in range(12)]
    plan = {"hook": "h", "context": "c", "payoff": "p", "credit": "x", "start": 0.7, "end": 26.2}
    total = tclips.render(str(src), plan, words, tmp_path / "o.mp4", fx=False)
    assert total < 15 and abs(total - (26.2 - 0.7 - (20.0 - 6.9 - tclips.KEEP_GAP))) < 0.01
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                        str(tmp_path / "o.mp4")], capture_output=True, text=True)
    assert abs(float(p.stdout) - total) < 0.2


def test_tabloid_is_the_default_style():
    """Roeland's pick in the style test (2026-11-13): new Shorts use the tabloid theme."""
    import inspect
    assert tclips.THEME == "tabloid" and inspect.signature(tclips.render).parameters["theme"].default == "tabloid"


def test_engagement_elements_render(tmp_path):
    """FX: hook zoom, freeze on the payoff (+FX_FREEZE), circle, loop fade, sound effects; plain stays as it was."""
    src = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:r=24:d=22",
                    "-f", "lavfi", "-i", "sine=d=22", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    words = [[f"w{i}{'.' if i % 5 == 4 else ''}", 1.0 + i * 0.5, 1.4 + i * 0.5] for i in range(36)]
    plan = {"hook": "h", "context": "c", "payoff": "p", "credit": "x", "start": 0.7, "end": 19.2}
    plain = tclips.render(str(src), plan, words, tmp_path / "plain.mp4", fx=False)
    fx = tclips.render(str(src), plan, words, tmp_path / "fx.mp4", fx=True, focus={"t": 12.0, "x": 0.5, "y": 0.5})
    assert abs(fx - plain - tclips.FX_FREEZE) < 0.01
    for f, want in (("plain.mp4", plain), ("fx.mp4", fx)):
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(tmp_path / f)], capture_output=True, text=True)
        assert abs(float(p.stdout) - want) < 0.25, f
    # the freeze lands on a word end, near the payoff point
    segs, out = tclips.tighten(words, 0.7, 19.2)
    frz = tclips.freeze_point(out, plain)
    assert any(abs(w[2] - frz) < 1e-6 for w in out) and abs(frz - max(plain - 5, plain * 0.65)) < 0.6
    assert runner.command({"id": "2026-11-13-tclipscompare-1", "name": "tclipscompare"})[-2:] == ["meta.tclips", "compare"]


def test_compare_takes_five_different_shorts():
    assert len(tclips.COMPARE) == 5 and len({n.split("-")[0] for n in tclips.COMPARE}) == 4


def test_effects_are_off_by_default():
    """Roeland 2026-11-13: he didn't like the engagement effects; they stay available behind fx=True."""
    assert tclips.FX is False


def test_example_lens_lengths_avoid_and_titles(tmp_path, monkeypatch):
    """The example round (Roeland 2026-11-13): weird-first prompt, film lengths 8-30 s, lines an
    earlier Short used are skipped, the title rule reaches the layer prompt, output in its own folder."""
    src = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=640x480:r=24:d=70",
                    "-f", "lavfi", "-i", "sine=d=70", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
    monkeypatch.setattr(tclips, "OUT", tmp_path / "out")
    monkeypatch.setattr(tx, "get", lambda s: {"origin": "whisper", "sentences": sents(17)})

    class FakeLLM:
        prompts = []

        def ask_json(self, prompt, cmd, retries=1, images=None):
            self.prompts.append(prompt)
            if "candidate moments" in prompt:
                return {"moments": [{"from": 1, "to": 3, "scores": {"weird": 5, "ad_safe": 5}},
                                    {"from": 8, "to": 10, "scores": {"weird": 4}}]}
            if "rank the best" in prompt:
                return {"ranked": ["M1"], "checks": [{"id": "M1", "ok": True}]}
            if "OUR layer" in prompt:
                return {"usable": True, "hook": "h", "context": "c", "payoff": "p.", "title": "STARE",
                        "credit": "c", "layer": ["context", "payoff_fact"]}
            return {"ok": True}

    class Settings:
        claude_cmd = "claude"
    llm = FakeLLM()
    out = tmp_path / "ex"
    out.mkdir()
    ex = {"lo": 8.0, "hi": 30.0, "min_tight": 8.0, "moments": tclips.WEIRD_FILM.format(lo=8, hi=30),
          "layer": tclips.TITLE_FILM, "avoid": [(0, 2)], "out": out, "prefix": "ex-"}
    made = tclips.make_source({"id": "f", "url": str(src), "title": "Film", "year": 1940}, "feature", 0,
                              Settings(), llm, per_source=2, bare_first=False, ex=ex)
    assert [m["short"] for m in made] == ["ex-feature-1a"]           # 1-3 overlaps the used lines 0-2
    assert made[0]["sentences"] == [8, 10] and made[0]["weird"] == 4
    assert 8 <= made[0]["lengths"]["ex-feature-1a.mp4"] <= 31
    assert (out / "ex-feature-1a.mp4").exists()
    assert "WEIRDEST" in llm.prompts[0] and "8-30 second" in llm.prompts[0]
    assert any("Cult Cinema Classics" in p for p in llm.prompts if "OUR layer" in p)


def test_open_newsreel_search_is_not_taken_for_a_checked_film(monkeypatch):
    """tclipsexamples-1 (2026-11-13) crashed: the newsreel searches start with title:( and were
    read as a checked-film entry."""
    q = tclips.KINDS["newsreel"]["searches"][0]
    monkeypatch.setattr(tclips, "search", lambda qq, rows=30: [{"identifier": "UN-1", "year": "1957"},
                                                               {"identifier": "UN-old", "year": "1957"}] if qq == q else [])
    monkeypatch.setattr(tclips, "archive_item", lambda i: {"url": f"u/{i}", "length": 400, "captions_url": None,
                                                           "meta": {"title": "Universal Newsreel 1957", "year": "1957"}})
    got = tclips.find_archive("newsreel", 3, [], [], skip={"UN-old"})
    assert [g["id"] for g in got] == ["UN-1"]


def test_runner_shares_only_finished_videos(tmp_path, monkeypatch):
    import os
    import time
    monkeypatch.setattr(runner, "OUT", tmp_path)
    start = time.time() - 5
    (tmp_path / "tclips" / "examples").mkdir(parents=True)
    (tmp_path / "tclips" / "work").mkdir()
    done = tmp_path / "tclips" / "examples" / "ex-newsreel-1a.mp4"
    done.write_bytes(b"x")
    (tmp_path / "tclips" / "work" / "render-ex-newsreel-2a.mp4").write_bytes(b"x")
    old = tmp_path / "tclips" / "old.mp4"
    old.write_bytes(b"x")
    os.utime(old, (start - 100, start - 100))
    assert runner.new_videos(start) == [done]
    assert runner.command({"id": "2026-11-13-tclipsexamples-2", "name": "tclipsexamples"})[-1] == "examples"


def test_examples_one_short_per_source_until_each_kind_is_full(tmp_path, monkeypatch):
    import json
    import sys
    import types
    monkeypatch.setattr(tclips, "OUT", tmp_path)
    monkeypatch.setattr(tclips, "EX_DIR", tmp_path / "examples")
    monkeypatch.setattr(tclips, "PLAN", tmp_path / "upload-plan.json")
    monkeypatch.setitem(sys.modules, "meta.common", types.SimpleNamespace(open_library=lambda: (None, None, None)))
    (tmp_path / "sources-feature.json").write_text(json.dumps([{"id": f"f{i}", "kind": "feature", "title": "F"}
                                                                for i in range(3)]))
    (tmp_path / "made.json").write_text(json.dumps([{"short": "feature-1a", "source": "f0", "sentences": [3, 6]}]))
    monkeypatch.setattr(tclips, "find_archive", lambda kind, n, t, d, skip=(): [
        {"id": f"{kind}{i}", "kind": kind, "title": "T"} for i in range(n)])
    monkeypatch.setattr(tx, "get", lambda s: {})
    calls = []

    def fake_make(s, kind, si, settings, llm, per, bare, ex=None):
        calls.append((s["id"], per, ex["avoid"], ex["lo"]))
        if s["id"] in ("newsreel1", "f1"):
            return []  # a source that gives nothing: a spare takes its place
        return [{"short": f"{ex['prefix']}{kind}-{si + 1}a", "source": s["id"], "sentences": [10, 12],
                 "lengths": {}, "kind": kind}]
    monkeypatch.setattr(tclips, "make_source", fake_make)
    made = tclips.examples()
    kinds = [m["kind"] for m in made]
    assert kinds.count("newsreel") == 4 and kinds.count("prelinger") == 3 and kinds.count("feature") == 3
    assert all(per == 1 for _, per, _, _ in calls)
    assert ("f0", 1, [(3, 6)], 8.0) in calls                       # film lines used before are avoided
    assert [m["source"] for m in made if m["kind"] == "feature"] == ["f0", "f2", "f0"]  # f1 gave none: 2nd from f0
    assert (tmp_path / "examples.json").exists()
