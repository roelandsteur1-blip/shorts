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
        assert abs(total - 18.0) < 0.01
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
    assert len(llm.prompts) == 2 + 2 * 2  # moments, rank, then layer + review per Short
