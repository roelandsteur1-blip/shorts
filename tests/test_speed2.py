"""Speed plan 2: one pipeline for several channels, downloads in the background, parallel uploads."""
import dataclasses
import threading

from conftest import fake_claude
from test_schedule_stats import cfg, fake_upload  # noqa: F401 - fixtures
from shorts import library as lib
from shorts import pipeline, progress


def test_make_many_shares_one_run_and_keeps_one_channel_per_video(library, settings, channel, monkeypatch):
    fake_claude(monkeypatch)
    other = dataclasses.replace(channel, id="space2", name="Space two")
    ready = pipeline.make_many(library, settings, [(channel, 1, None), (other, 1, None)])
    assert ready == {"space": 1, "space2": 1}
    heroes = library.execute("""SELECT s.channel, sc.asset_id FROM shorts s JOIN scenes sc ON sc.id=s.hero_scene_id
                                 WHERE s.status='rendered'""").fetchall()
    # The second channel's heroes are picked after the first one's scripts are saved: other video.
    assert {h["channel"] for h in heroes} == {"space", "space2"}
    assert len({h["asset_id"] for h in heroes}) == 2


def test_top_up_counts_shorts_still_being_made(conn, channel):
    for st in ("scripted", "rendered", "failed", "scheduled"):
        conn.execute("INSERT INTO shorts (channel, format_id, status) VALUES (?, ?, ?)", (channel.id, "f", st))
    conn.commit()
    orders, enough = pipeline.top_up_orders(conn, [channel], 3)
    assert [(ch.id, n) for ch, n, _ in orders] == [("space", 1)] and not enough
    orders, enough = pipeline.top_up_orders(conn, [channel], 2)
    assert not orders and enough


def test_prefetch_downloads_each_source_in_its_own_thread(conn, settings, channel, monkeypatch):
    from shorts.sources import common
    settings.restock_below = 100
    ch = dataclasses.replace(channel, sources=["nasa", "commons"])
    seen, both = [], threading.Barrier(2, timeout=10)

    def ingest(c, s, src, queries, per_term=10, **kw):
        progress.report(1, 1, "should be muted")
        both.wait()  # only passes when both sources download at the same time
        seen.append(src)
        return 1
    monkeypatch.setattr(common, "ingest", ingest)
    dl = lib.prefetch(conn, settings, [(ch, 2)])
    assert dl is not None
    assert lib.finish_prefetch(conn, settings, dl) == 2
    assert sorted(seen) == ["commons", "nasa"]


def test_prefetch_skips_when_stock_lasts(conn, settings, channel, monkeypatch):
    settings.restock_below = 0
    assert lib.prefetch(conn, settings, [(channel, 2)]) is None


def test_progress_mute_is_per_thread(capsys):
    def quiet():
        progress.mute()
        progress.report(1, 2, "hidden")
    t = threading.Thread(target=quiet)
    t.start()
    t.join()
    progress.report(2, 2, "shown")
    out = capsys.readouterr().out
    assert "shown" in out and "hidden" not in out


def test_upload_all_divides_the_quota(conn, settings, cfg, fake_upload):
    from test_schedule_stats import _FakeYouTube, add_short
    from shorts import upload
    space = cfg / "channels" / "space.toml"
    (cfg / "channels" / "body.toml").write_text(space.read_text().replace('id = "space"', 'id = "body"'))
    fake_upload(_FakeYouTube(ok=100))
    settings.upload_quota = 2
    for _ in range(3):
        add_short(conn, video_path="x.mp4")
    for _ in range(3):
        add_short(conn, video_path="x.mp4", channel="body")
    out = upload.upload_all(conn, settings)
    # Channels in name order: body gets the quota that is left, space waits for tomorrow.
    assert out["body"].startswith("uploaded 2") and out["space"].startswith("uploaded 0")
    assert conn.execute("SELECT COUNT(*) FROM shorts WHERE status='scheduled'").fetchone()[0] == 2
