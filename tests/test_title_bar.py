"""The title bar at the top of a Short (render/looks) and the screen title the script writes."""
from dataclasses import replace

from shorts import channels as chans, hero, looks, render, script


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
    assert chans.validate(chans.raw(s, "aaa"), {"space"}) == []
    bad = chans.raw(s, "aaa")
    bad["style"]["title_bar"] = "sideways"
    assert any("title bar" in e for e in chans.validate(bad, {"space"}))
