"""Add-channel wizard, settings form, Claude draft and the YouTube channel check."""
import json
import shutil
import tomllib

import pytest

from shorts import channels as chans, llm, upload
from shorts.config import ROOT, load_channel


@pytest.fixture
def cfg(tmp_path, settings):
    d = tmp_path / "config"
    shutil.copytree(ROOT / "config", d, ignore=shutil.ignore_patterns("local.toml", "*.bak"))
    settings.config_root = d
    return d


@pytest.fixture
def client(settings, cfg):
    from shorts.web import create_app
    return create_app(settings, start_runner=False).test_client()


DRAFT = {"hashtags": ["#history", "#shorts"], "categories": ["history", "war", "made_up"],
         "sources": ["prelinger", "loc", "nope"], "search_terms": ["polygoon", "newsreel"],
         "hero_query": "a dramatic old newsreel moment",
         "formats": [{"id": "Then & Now", "title": "Then and now", "brief": "Compare the past with today."},
                     {"id": "story", "title": "The story", "brief": "Tell the story behind the shot."}]}


def form_for(data, n_formats=3):
    form = {"name": data["name"], "id": data.get("id", ""), "language": "nl", "niche": data["niche"],
            "hashtags": "#history shorts", "search_terms": "newsreel\npolygoon", "categories": ["history", "war"],
            "sources": ["prelinger", "loc"], "min_seconds": "15", "max_seconds": "30", "uploads_per_day": "1",
            "publish_times_utc": "16:00", "format_count": str(n_formats), "hero_query": ""}
    form.update({"format_id_0": "then_now", "format_title_0": "Then and now",
                 "format_brief_0": "Compare the past\r\nwith today.", "format_enabled_0": "1"})
    return form


def test_wizard_creates_a_channel(client, settings, cfg, monkeypatch):
    assert b"create_channel" in client.get("/channels/new").data
    assert b"Draft with Claude" in client.get("/channels/new/describe").data

    monkeypatch.setattr(llm, "ask", lambda *a, **k: json.dumps(DRAFT))
    d = client.post("/channels/draft", data={"name": "Oud Nieuws", "niche": "Dutch history", "language": "nl"}).get_json()
    assert d["categories"] == ["history", "war"] and d["sources"] == ["prelinger", "loc"]
    assert [f["id"] for f in d["formats"]] == ["then_now", "story"]
    assert client.post("/channels/draft", data={"name": ""}).status_code == 400

    r = client.post("/channels/new", data=form_for({"name": "Oud Nieuws", "id": "oudnieuws", "niche": "Dutch history"}))
    assert r.status_code == 302 and r.headers["Location"].endswith("/channels/oudnieuws?created=1")
    ch = load_channel("oudnieuws", cfg)
    assert (ch.language, ch.categories, ch.sources, ch.hashtags) == ("nl", ["history", "war"], ["prelinger", "loc"], ["#history", "#shorts"])
    assert ch.formats == [{"id": "then_now", "title": "Then and now", "brief": "Compare the past\nwith today.", "enabled": True}]
    assert ch.style["font"] == "Montserrat ExtraBold" and ch.hero_query is None
    # The same id twice is refused, and so is a broken form.
    assert client.post("/channels/new", data=form_for({"name": "X", "id": "oudnieuws", "niche": "n"})).status_code == 400
    bad = form_for({"name": "Y", "id": "y_ch", "niche": "n"}) | {"publish_times_utc": "25:00", "format_enabled_0": ""}
    page = client.post("/channels/new", data=bad)
    assert page.status_code == 400 and b"upload times" in page.data and b"at least one format" in page.data
    assert not (cfg / "channels" / "y_ch.toml").exists()

    page = client.get("/channels/oudnieuws").data
    assert b"Connect YouTube" in page and b"no tracks yet" in page and b"Try it" in page
    assert b"Oud Nieuws" in client.get("/").data


def test_settings_keep_style_and_backup(client, cfg):
    before = tomllib.loads((cfg / "channels" / "space.toml").read_text())
    form = form_for({"name": "Space Shorts", "niche": "space exploration and astronomy"}, n_formats=1)
    form.update(categories=["space"], sources=["nasa", "internet_archive"], language="en", auto_run="1")
    r = client.post("/channels/space", data=form)
    assert r.status_code == 302
    after = tomllib.loads((cfg / "channels" / "space.toml").read_text())
    assert after["style"] == before["style"] and after["id"] == "space"
    assert after["sources"] == ["nasa", "internet_archive"] and after["auto_run"] is True
    assert (cfg / "channels" / "space.toml.bak").read_text().startswith("# One file per channel")


class FakeYT:
    def __init__(self, cid, title="Space Facts"):
        self.cid, self.title = cid, title

    def channels(self):
        return self

    def list(self, **kw):
        assert kw == {"part": "snippet", "mine": True}
        return self

    def execute(self):
        return {"items": [{"id": self.cid, "snippet": {"title": self.title,
                                                       "thumbnails": {"default": {"url": "https://yt/x.jpg"}}}}]}


def test_connect_and_confirm(client, settings, cfg, conn, monkeypatch):
    monkeypatch.setattr(upload, "youtube", lambda s, ch: FakeYT("UC123"))
    found = upload.connect(settings, load_channel("space", cfg))
    assert found == {"id": "UC123", "title": "Space Facts", "thumbnail": "https://yt/x.jpg"}
    page = client.get("/channels/space").data
    assert b"Space Facts" in page and b"that's the one" in page
    client.post("/channels/space/confirm")
    assert load_channel("space", cfg).youtube_channel_id == "UC123"
    client.post("/channels/space/connect", data={"fresh": "1"})
    job = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert json.loads(job["command"]) == ["connect", "--fresh"] and job["heavy"] == 0
    client.post("/channels/space/try")
    assert json.loads(conn.execute("SELECT command FROM jobs ORDER BY id DESC").fetchone()[0]) == ["make", "-n", "3"]


def test_upload_refuses_another_channel(channel):
    channel.youtube_channel_id = "UC123"
    upload.check_channel(FakeYT("UC123"), channel)
    with pytest.raises(RuntimeError, match="not the one connected"):
        upload.check_channel(FakeYT("UC999", "Other channel"), channel)
    channel.youtube_channel_id = None
    upload.check_channel(FakeYT("UC999"), channel)  # not confirmed yet: warns only


def test_slug_and_validate():
    assert chans.slug("Oud Nieuws!") == "oud_nieuws" and chans.slug("1950s") == "ch_1950s"
    data = chans.new_channel("abc", "ABC", "en", "n")
    assert chans.validate(data, set()) == []
    assert any("unknown categories" in e for e in chans.validate(data | {"categories": ["x"]}, set()))
    assert any("unknown fields" in e for e in chans.validate(data | {"colour": 1}, set()))
