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




DRAFT = {"niche_tags": ["#history", "old news", "newsreels", "extra"], "categories": ["history", "war", "made_up"],
         "sources": ["prelinger", "commons", "nope"], "search_terms": ["polygoon", "newsreel"],
         "hero_query": "a dramatic old newsreel moment",
         "formats": [{"id": "Then & Now", "title": "Then and now", "brief": "Compare the past with today."},
                     {"id": "story", "title": "The story", "brief": "Tell the story behind the shot."}]}


def form_for(data, n_formats=3):
    form = {"name": data["name"], "id": data.get("id", ""), "language": "nl", "niche": data["niche"],
            "niche_tags": "history, old news\nnewsreels, fourth", "search_terms": "newsreel\npolygoon", "categories": ["history", "war"],
            "sources": ["prelinger", "commons"], "min_seconds": "15", "max_seconds": "30", "uploads_per_day": "1",
            "publish_times_utc": "16:00", "format_count": str(n_formats), "hero_query": ""}
    form.update({"format_id_0": "then_now", "format_title_0": "Then and now",
                 "format_brief_0": "Compare the past\r\nwith today.", "format_enabled_0": "1"})
    return form






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
