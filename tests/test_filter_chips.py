"""Filter chips: show only channels of some video types and/or topics, on every page (8 Nov 2026)."""
import dataclasses
import shutil
from pathlib import Path

import pytest

from shorts import channels as chans
from shorts.web import create_app


@pytest.fixture
def app_settings(settings, tmp_path):
    cfg = tmp_path / "cfg"
    shutil.copytree(Path(chans.__file__).parent.parent / "config", cfg)
    for seed in ("warplanecards", "retrotechmix", "namethatwarplane"):
        shutil.copy(cfg / "channel-seeds" / f"{seed}.toml", cfg / "channels" / f"{seed}.toml")
    return dataclasses.replace(settings, config_root=cfg)


@pytest.fixture
def client(app_settings, conn):
    for ch in ("space", "warplanecards", "retrotechmix", "namethatwarplane"):
        conn.execute("INSERT INTO shorts (channel, format_id, title, status, duration) VALUES (?, 'f', ?, 'rendered', 12)",
                     (ch, f"title-{ch}"))
    conn.commit()
    return create_app(app_settings, start_runner=False).test_client()


def test_chips_show_types_and_topics_with_counts(client):
    page = client.get("/review").get_data(as_text=True)
    assert 'class="chipbar"' in page
    for label in ("Classic", "Text card", "Quiz", "Mix", "Warplanes", "Retro tech", "Space"):
        assert label in page
    assert "Ranking <span" not in page  # no ranking channel here: no chip


def test_review_filters_by_topic_and_type(client):
    page = client.get("/review?f=1&topic=warplane").get_data(as_text=True)
    assert "title-warplanecards" in page and "title-namethatwarplane" in page
    assert "title-space" not in page and "title-retrotechmix" not in page
    page = client.get("/review?f=1&topic=warplane&type=quiz").get_data(as_text=True)
    assert "title-namethatwarplane" in page and "title-warplanecards" not in page
    page = client.get("/review?f=1&type=mix&type=classic").get_data(as_text=True)
    assert "title-space" in page and "title-retrotechmix" in page and "title-warplanecards" not in page


def test_pick_is_remembered_across_pages_and_cleared(client):
    client.get("/review?f=1&topic=retrotech")
    page = client.get("/review").get_data(as_text=True)  # cookie
    assert "title-retrotechmix" in page and "title-space" not in page
    assert "1 of 4 channels" in page
    home = client.get("/").get_data(as_text=True)
    assert "Retro Tech Mix" in home and "Warplane Cards" not in home
    client.get("/review?f=1")  # "All channels"
    page = client.get("/review").get_data(as_text=True)
    assert "title-space" in page and "title-retrotechmix" in page


def test_chosen_channel_wins_over_the_chips(client):
    client.get("/review?f=1&topic=retrotech")
    page = client.get("/review?channel=space").get_data(as_text=True)
    assert "title-space" in page and "title-retrotechmix" not in page


def test_accept_all_only_touches_the_filtered_channels(client, conn):
    client.get("/review?f=1&topic=warplane")
    client.post("/review/approve-all", data={"channel": ""})
    status = dict(conn.execute("SELECT channel, status FROM shorts").fetchall())
    assert status == {"space": "rendered", "warplanecards": "approved", "retrotechmix": "rendered",
                      "namethatwarplane": "approved"}


def test_next_short_stays_in_the_filter(client, conn):
    ids = dict(conn.execute("SELECT channel, id FROM shorts").fetchall())
    client.get("/review?f=1&topic=warplane")
    resp = client.post(f"/short/{ids['warplanecards']}", data={"action": "approve", "title": "t"})
    assert resp.headers["Location"].endswith(f"/short/{ids['namethatwarplane']}")


@pytest.mark.parametrize("path", ["/", "/review", "/library", "/schedule", "/stats", "/voices"])
def test_every_list_page_has_chips_and_filters(client, path, monkeypatch):
    from shorts.web import home, schedule
    monkeypatch.setattr(home, "youtube_schedule", lambda chs: {})
    monkeypatch.setattr(schedule, "youtube_schedule", lambda chs: {})
    resp = client.get(path + "?f=1&type=textcard")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert 'class="chipbar"' in page and "1 of 4 channels" in page
