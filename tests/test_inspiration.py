"""Competitor research (shorts/inspiration.py) and the title bar at the top (render/looks)."""
import json
from dataclasses import replace

from conftest import fake_claude
from shorts import channels as chans, hero, inspiration, library as lib, llm, looks, pipeline, render, script


class _Req:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeYouTube:
    """Two channels: @rockets (Shorts playlist) and UCbbb... (no Shorts playlist, long videos mixed in)."""
    A, B = "UC" + "a" * 22, "UC" + "b" * 22

    def __init__(self):
        self.calls = []
        self.videos_by_id = {}
        for n in range(6):
            self.videos_by_id[f"a{n}"] = (f"Rocket fail number {n}", 1000 * (n + 1), "PT45S")
        self.videos_by_id["b0"] = ("Why the Moon is drifting away", 90000, "PT30S")
        self.videos_by_id["b1"] = ("Full lecture", 500000, "PT1H2M")

    def channels(self):
        outer = self

        class C:
            def list(self, part, **kw):
                outer.calls.append(("channels", kw))
                if kw.get("forHandle") == "@rockets" or kw.get("id") == outer.A:
                    return _Req({"items": [{"id": outer.A, "snippet": {"title": "Rockets"}}]})
                if kw.get("id") == outer.B:
                    return _Req({"items": [{"id": outer.B, "snippet": {"title": "Moon facts"}}]})
                return _Req({"items": []})
        return C()

    def playlistItems(self):
        outer = self

        class P:
            def list(self, part, playlistId, maxResults, pageToken=None):
                outer.calls.append(("playlistItems", playlistId))
                ids = {"UUSH" + outer.A[2:]: [f"a{n}" for n in range(6)], "UU" + outer.B[2:]: ["b0", "b1"]}.get(playlistId)
                if ids is None:
                    from googleapiclient.errors import HttpError

                    class Resp(dict):
                        status = 404
                        reason = "not found"
                    raise HttpError(Resp(), b"{}")
                return _Req({"items": [{"contentDetails": {"videoId": i}} for i in ids]})
        return P()

    def videos(self):
        outer = self

        class V:
            def list(self, part, id, maxResults=50):
                outer.calls.append(("videos", id))
                items = []
                for vid in id.split(","):
                    title, views, dur = outer.videos_by_id[vid]
                    items.append({"id": vid, "snippet": {"title": title, "publishedAt": "2026-10-01T00:00:00Z"},
                                  "statistics": {"viewCount": str(views)}, "contentDetails": {"duration": dur}})
                return _Req({"items": items})
        return V()


# Claude may still send topics: they must never reach scripts, footage picking or downloads.
BANK = {"topics": [{"topic": "Rocket launches that went wrong", "angle": "danger", "visual": "rocket launch at night",
                    "search": "rocket failure", "evidence": "6000 views"}],
        "hooks": ["This [thing] almost [disaster]"], "titles": ["The [adjective] [thing] ever"],
        "format": ["under 30 seconds", "answer the hook in the last line"], "avoid": ["lists"]}


def _fake_research_claude(monkeypatch, prompts):
    def ask(prompt, claude_cmd="claude", timeout=600):
        prompts.append(prompt)
        return json.dumps(BANK)
    monkeypatch.setattr(llm, "ask", ask)


def test_parse_ref():
    assert inspiration.parse_ref("https://www.youtube.com/@Rockets/shorts") == ("handle", "@Rockets")
    assert inspiration.parse_ref("rockets") == ("handle", "@rockets")
    assert inspiration.parse_ref("https://youtube.com/channel/" + FakeYouTube.A) == ("id", FakeYouTube.A)
    assert inspiration.parse_ref(FakeYouTube.B) == ("id", FakeYouTube.B)
    assert inspiration.parse_ref("https://www.youtube.com/user/oldname") == ("user", "oldname")
    assert inspiration.seconds("PT1M5S") == 65 and inspiration.seconds("PT45S") == 45 and inspiration.seconds("P1DT1H") == 90000


def test_refresh_builds_idea_bank(conn, settings, channel, monkeypatch):
    prompts = []
    _fake_research_claude(monkeypatch, prompts)
    yt = FakeYouTube()
    ch = replace(channel, inspiration=["@rockets", "https://www.youtube.com/channel/" + FakeYouTube.B, "@missing"])
    assert inspiration.stale(conn, ch)
    r = inspiration.refresh(conn, settings, ch, yt=yt)
    assert r == {"patterns": 4, "shorts": 7, "channels": 2, "failed": ["@missing"]}
    # Shorts playlist used when there is one; without it, uploads minus long videos.
    titles = {row[0] for row in conn.execute("SELECT title FROM inspiration_videos WHERE channel='space'")}
    assert "Full lecture" not in titles and "Why the Moon is drifting away" in titles
    assert '6,000 views (1.7x median)' in prompts[0] and "Never copy a title" in prompts[0]
    assert '"topics"' not in prompts[0] and "our niche alone decides" in prompts[0]
    assert not inspiration.stale(conn, ch)
    assert "topics" not in inspiration.bank(conn, "space")
    note = inspiration.prompt_note(conn, "space")
    assert "This [thing] almost [disaster]" in note and "under 30 seconds" in note
    assert "Rocket" not in note and "niche alone" in note
    # Taking a channel off the list drops its Shorts on the next refresh.
    inspiration.refresh(conn, settings, replace(ch, inspiration=["@rockets"]), yt=yt)
    assert conn.execute("SELECT COUNT(*) FROM inspiration_videos WHERE source_channel=?", (FakeYouTube.B,)).fetchone()[0] == 0


def test_refresh_stale_never_raises(settings, channel, monkeypatch):
    ch = replace(channel, inspiration=["@rockets"])
    out = inspiration.refresh_stale(settings, [ch, channel])  # no login in the test data folder
    assert list(out) == ["space"] and out["space"].startswith("not refreshed")


def test_bank_steers_scripts_only_never_topics(library, settings, channel, monkeypatch):
    conn = library
    ch = replace(channel, inspiration=["@rockets"])
    # An old bank (topics, no format traits) is redone on the next Make.
    old = {k: v for k, v in BANK.items() if k != "format"}
    conn.execute("INSERT INTO idea_banks (channel, data, videos, made_at) VALUES ('space', ?, 7, datetime('now'))",
                 (json.dumps(old),))
    assert inspiration.stale(conn, ch)
    conn.execute("UPDATE idea_banks SET data=? WHERE channel='space'", (json.dumps(BANK),))
    conn.commit()
    assert not inspiration.stale(conn, ch)
    # Script prompt carries hook, title and format patterns, but not their topics.
    heroes = [script.hero_context(conn, s) for s in hero.candidates(conn, settings, "space", 1)]
    prompt = script.build_prompt(ch, ch.enabled_formats, heroes, [], inspiration.prompt_note(conn, "space"))
    assert "Hook patterns: This [thing] almost [disaster]" in prompt and "Rocket launches" not in prompt
    # Restock searches only the channel's own terms.
    without = lib.restock_plan(conn, settings, channel, need=10 ** 6)
    assert lib.restock_plan(conn, settings, ch, need=10 ** 6) == without
    assert not any("rocket failure" in qs for _, qs in without)


def test_make_waits_for_stale_research(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    ch = replace(channel, inspiration=["@rockets"])
    seen = []
    monkeypatch.setattr(inspiration, "refresh_stale", lambda s, chs: seen.extend(c.id for c in chs) or {})
    pipeline.make_many(library, settings, [(ch, 1, None)], restock=False)
    assert seen == ["space"]


def test_title_bar_in_ass_and_sizes_to_two_lines():
    blocks = [{"text": "Hook *here*", "seconds": 3.0}, {"text": "Then this", "seconds": 2.0}]
    ass = render.build_ass(blocks, {"look": "classic", "title_bar": "always"}, "Saturn's *vanishing* rings")
    style = next(x for x in ass.splitlines() if x.startswith("Style: Title"))
    assert ",8,60,200,300," in style  # top centre, caption column, below YouTube's top icons
    title = next(x for x in ass.splitlines() if ",Title," in x and x.startswith("Dialogue"))
    assert "0:00:04.96" in title and r"\c&H0000D7FF" in title  # whole Short, yellow key word
    hook_only = render.build_ass(blocks, {"look": "classic", "title_bar": "hook"}, "A title")
    assert "0:00:02.96,Title" in hook_only
    assert ",Title," not in render.build_ass(blocks, {"look": "classic", "title_bar": "off"}, "A title").split("[Events]")[1]
    assert ",Title," not in render.build_ass(blocks, {"look": "classic"}, None).split("[Events]")[1]
    long = "The strangest rocket failures ever filmed"  # 40 characters: the most a screen title has
    st = looks.resolve({"look": "classic"})
    size = looks.title_size(long, {"look": "classic"})
    assert size <= round(88 * looks.TITLE_SCALE) and looks.line_count(long, st | {"font_size": size}) <= 2
    assert looks.title_size("Saturn's *rings*", {"look": "classic"}) == round(88 * looks.TITLE_SCALE)
    longer = "The strangest and most dangerous rocket failures"
    size = looks.title_size(longer, {"look": "classic"})
    assert size < round(88 * looks.TITLE_SCALE) and looks.line_count(longer, st | {"font_size": size}) <= 2


def test_screen_title_cleaned_and_saved(library, settings, channel, monkeypatch):
    assert script.clean_screen_title("  The *loudest*\n sound ") == "The *loudest* sound"
    assert script.clean_screen_title("") is None
    cut = script.clean_screen_title("A really very long title that keeps *going* and going on forever")
    assert len(cut) <= script.SCREEN_TITLE_CHARS and cut.count("*") % 2 == 0
    prompt = script.build_prompt(channel, channel.enabled_formats, [], [])
    assert '"screen_title"' in prompt and "Screen title:" in prompt
    off = replace(channel, style=channel.style | {"title_bar": "off"})
    assert "screen_title" not in script.build_prompt(off, off.enabled_formats, [], [])
    h = script.hero_context(library, hero.candidates(library, settings, "space", 1)[0])
    data = {"blocks": [{"text": "One two three"}, {"text": "Four five"}, {"text": "Six seven"}],
            "title": "T", "screen_title": "Saturn's *rings*"}
    sid = script.save_script(library, channel, "what_youre_seeing", h, data)
    assert library.execute("SELECT screen_title FROM shorts WHERE id=?", (sid,)).fetchone()[0] == "Saturn's *rings*"


def test_title_bars_assigned_in_turn(tmp_path, settings, channel):
    from shorts import tomlw
    cfg = tmp_path / "cfg"
    (cfg / "channels").mkdir(parents=True)
    for cid in ("aaa", "bbb", "ccc"):
        tomlw.write(cfg / "channels" / f"{cid}.toml", chans.new_channel(cid, cid, "en", "space"), chans.HEADER)
    s = replace(settings, config_root=cfg)
    from shorts.config import load_channel
    chs = [load_channel(c, cfg) for c in ("aaa", "bbb", "ccc")]
    looks.assign_missing(s, chs)
    assert [c.style["title_bar"] for c in chs] == ["always", "hook", "always"]
    assert load_channel("bbb", cfg).style["title_bar"] == "hook"  # saved in its config
    assert chans.validate(chans.raw(s, "aaa") | {"inspiration": ["@x"]}, {"space"}) == []
    bad = chans.raw(s, "aaa")
    bad["style"]["title_bar"] = "sideways"
    assert any("title bar" in e for e in chans.validate(bad, {"space"}))
