"""Changes from the Shorts research (research/shorts-studies.md, 18 Oct 2026)."""
import json
from dataclasses import replace

from conftest import fake_claude
from shorts import channels as chans, db, formats, hero, llm, looks, pipeline, render, script


def test_looks_fixed_yellow_and_safe_margins():
    for name in looks.LOOK_NAMES:
        st = looks.resolve({"look": name, "highlight_color": "&H000000FF"})
        assert st["highlight_color"] == looks.HIGHLIGHT
    ass = render.build_ass([{"text": "Hello *world*", "seconds": 2.0}], {"look": "anton_box"})
    body = next(line for line in ass.splitlines() if line.startswith("Style: Body"))
    assert "Anton" in body and ",3,16,0,2,60,200," in body  # box style, text box x 60-880
    ass = render.build_ass([{"text": "Hi", "seconds": 2.0}], {"look": "classic"})
    assert ",1,6,3,2,60,200," in next(x for x in ass.splitlines() if x.startswith("Style: Body"))


def test_custom_look_keeps_own_font():
    st = looks.resolve({"look": "custom", "font": "Arial Black", "font_size": 70})
    assert (st["font"], st["font_size"]) == ("Arial Black", 70)


def test_blocks_never_wrap_past_two_lines():
    st = looks.resolve({"look": "classic"})
    text = "Apollo eleven landed on the Moon in nineteen sixty nine and nobody saw it live"
    parts = script.split_block(text, style=st)
    assert " ".join(parts) == text
    assert all(len(p.split()) <= script.MAX_WORDS and looks.line_count(p, st) <= 2 for p in parts)


def test_every_channel_gets_a_different_format_mix(channel):
    mixes = set()
    for cid in ["space", "history", "body", "ocean", "weather"]:
        ch = replace(channel, id=cid, formats=channel.formats[:1])
        fmts = formats.for_channel(ch)
        assert len(fmts) == formats.FORMATS_PER_CHANNEL and fmts[0]["id"] == "what_youre_seeing"
        assert len({f["id"] for f in fmts}) == 3
        mixes.add(tuple(f["id"] for f in fmts))
    assert len(mixes) > 1
    assert formats.for_channel(replace(channel, library_formats=False)) == channel.enabled_formats
    assert channel.format("myth_vs_fact")["title"] == "Myth vs fact"


def test_prompt_offers_formats_and_saves_claudes_pick(library, settings, channel, monkeypatch):
    heroes = hero.candidates(library, settings, channel.id, 1)
    seen = {}

    def ask(prompt, *a, **k):
        seen["prompt"] = prompt
        return json.dumps([{"hero": 1, "format": "myth_vs_fact", "topic": "t",
                            "blocks": [{"text": "a b c"}, {"text": "d e f", "visual": "x"}, {"text": "g h", "visual": "y"}]}])
    monkeypatch.setattr(llm, "ask", ask)
    ch = replace(channel, formats=[f for f in channel.formats if f["id"] == "what_youre_seeing"])
    ch.formats.append({"id": "myth_vs_fact", "title": "Myth vs fact", "brief": "b"})
    sid = script.write_scripts(library, settings, ch, None, heroes)[0]
    assert '"format": "<format id>"' in seen["prompt"] and "LAST block is the payoff" in seen["prompt"]
    assert library.execute("SELECT format_id FROM shorts WHERE id=?", (sid,)).fetchone()[0] == "myth_vs_fact"


def test_three_hashtags_ending_in_shorts(channel):
    ch = replace(channel, hashtags=["#space", "#nasa", "#astronomy", "#shorts"])
    assert render.hashtags(ch) == ["#space", "#nasa", "#shorts"]
    assert render.hashtags(replace(channel, hashtags=[])) == ["#shorts"]


def test_shorts_capped_at_a_minute(channel):
    assert replace(channel, max_seconds=90).max_seconds == 60
    data = chans.new_channel("abc", "A", "en", "things")
    data["max_seconds"] = 90
    assert any("60" in e for e in chans.validate(data, set()))


def test_assign_missing_gives_distinct_looks_without_writing_for_test_settings(settings, channel):
    a = replace(channel, id="a", style={})
    b = replace(channel, id="b", style={})
    looks.assign_missing(settings, [a, b])
    assert a.style["look"] and b.style["look"] and a.style["look"] != b.style["look"]


def test_claimed_footage_is_never_used_again(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    sid = pipeline.make(library, settings, channel, count=1)[0]
    used = [r[0] for r in library.execute(
        "SELECT DISTINCT sc.asset_id FROM blocks b JOIN scenes sc ON sc.id=b.scene_id WHERE b.short_id=?", (sid,))]
    db.log_claim(library, sid, used, "Some Broadcaster")
    assert library.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == len(used)
    picked = hero.candidates(library, settings, channel.id, 20)
    owners = {r[0] for r in library.execute(
        f"SELECT asset_id FROM scenes WHERE id IN ({','.join('?' * len(picked))})", picked)} if picked else set()
    assert not owners & set(used)


def test_broll_not_reused_across_channels(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    sid = pipeline.make(library, settings, channel, count=1)[0]
    used = db.used_scene_ids(library, channel.id)
    assert used and used <= db.used_scene_ids(library, None)
    assert db.used_scene_ids(library, "other") == set()
    assert sid
