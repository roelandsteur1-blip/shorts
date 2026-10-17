"""Phase 5 and 6: edit text and re-render, Schedule page and upload quota, daily auto-run, Stats page."""
import json
import shutil
from datetime import datetime, timezone

import pytest

from shorts import daily, script, upload
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


def add_short(conn, status="approved", title="A Short", **fields):
    cols = {"channel": "space", "format_id": "what_youre_seeing", "title": title, "status": status,
            "tags": "[]", "hook": "first", **fields}
    sid = conn.execute(f"INSERT INTO shorts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                       list(cols.values())).lastrowid
    conn.executemany("INSERT INTO blocks (short_id, idx, text, seconds, visual) VALUES (?,?,?,?,?)",
                     [(sid, i, t, 5.0, "hero" if i == 0 else "x") for i, t in enumerate(["first", "second *word*", "third"])])
    conn.commit()
    return sid


def test_edit_blocks(conn, channel):
    sid = add_short(conn, status="rendered")
    assert not script.edit_blocks(conn, channel, sid, {0: "first", 1: " second  *word* "})  # nothing really changed
    assert script.edit_blocks(conn, channel, sid, {0: "A new and much better hook line here", 2: "end"})
    blocks = conn.execute("SELECT text, seconds FROM blocks WHERE short_id=? ORDER BY idx", (sid,)).fetchall()
    assert blocks[0]["text"] == "A new and much better hook line here"
    assert sum(b["seconds"] for b in blocks) >= channel.min_seconds
    s = conn.execute("SELECT hook, edited, duration FROM shorts WHERE id=?", (sid,)).fetchone()
    assert s["hook"].startswith("A new") and s["edited"] == 1
    assert abs(s["duration"] - sum(b["seconds"] for b in blocks)) < 0.01


def test_edit_page_queues_rerender(client, conn):
    sid = add_short(conn, status="rendered")
    page = client.get(f"/short/{sid}").data
    assert b"Save text and re-render" in page and b'name="block_1"' in page
    r = client.post(f"/short/{sid}/blocks", data={"block_0": "first", "block_1": "a better second line", "block_2": "third"})
    job = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert json.loads(job["command"]) == ["rerender", str(sid)] and r.headers["Location"].endswith(f"/jobs/{job['id']}")
    # Uploaded Shorts can't be edited here any more.
    done = add_short(conn, status="scheduled", youtube_id="abc")
    assert client.post(f"/short/{done}/blocks", data={"block_0": "x"}).status_code == 400
    assert b"Save text and re-render" not in client.get(f"/short/{done}").data


def test_schedule_page(client, conn, settings):
    a = add_short(conn, title="Waiting one")
    add_short(conn, status="scheduled", title="Next week one", youtube_id="yt1", publish_at="2099-01-02T15:00:00Z",
              uploaded_at=upload.iso(datetime.now(timezone.utc)))
    add_short(conn, status="scheduled", title="Old one", youtube_id="yt0", publish_at="2020-01-02T15:00:00Z")
    page = client.get("/schedule").data.decode()
    assert "Waiting one" in page and "Schedule approved now" in page
    assert "2099-01-02" in page and "youtu.be/yt1" in page and "Old one" in page
    assert "Uploads today: <b class=\"\">1 of 100</b>" in page
    assert "Daily auto-run" in page
    client.post("/jobs/start", data={"command": "upload", "channel": "space"})
    assert json.loads(conn.execute("SELECT command FROM jobs ORDER BY id DESC").fetchone()[0]) == ["upload"]
    assert client.post("/jobs/start", data={"command": "rm", "channel": "space"}).status_code == 400


def test_pacific_midnight():
    # Summer (PDT, UTC-7): 16 Oct 2026 10:00 UTC is 03:00 in California -> midnight = 07:00 UTC.
    assert upload.iso(upload.pacific_midnight(datetime(2026, 10, 16, 10, tzinfo=timezone.utc))) == "2026-10-16T07:00:00Z"
    # Before 07:00 UTC it's still the previous day there.
    assert upload.iso(upload.pacific_midnight(datetime(2026, 10, 16, 5, tzinfo=timezone.utc))) == "2026-10-15T07:00:00Z"
    # Winter (PST, UTC-8).
    assert upload.iso(upload.pacific_midnight(datetime(2026, 12, 1, 12, tzinfo=timezone.utc))) == "2026-12-01T08:00:00Z"


def test_uploads_today_counts_all_channels(conn):
    now = datetime(2026, 10, 16, 20, tzinfo=timezone.utc)
    add_short(conn, uploaded_at="2026-10-16T08:00:00Z")
    add_short(conn, uploaded_at="2026-10-16T06:59:00Z")  # before the reset
    add_short(conn, uploaded_at="2026-10-16T19:00:00Z", channel="other")
    assert upload.uploads_today(conn, now) == 2


def test_daily_run(conn, settings, cfg, monkeypatch):
    import shorts.pipeline
    import shorts.upload as up
    path = cfg / "channels" / "space.toml"
    path.write_text(path.read_text().replace('id = "space"', 'id = "space"\nauto_run = true'))
    made, uploaded = [], []
    monkeypatch.setattr(shorts.pipeline, "make", lambda c, s, ch, n: made.append((ch.id, n)) or [1, 2])
    monkeypatch.setattr(up, "upload_approved", lambda c, s, ch: uploaded.append(ch.id) or ["yt"])
    assert daily.run(conn, settings) == {"space": "made 2, scheduled 1"}
    assert made == [("space", 2)]
    # A big review backlog: no new Shorts, approved ones are still scheduled.
    for _ in range(daily.BACKLOG_DAYS * 2):
        add_short(conn, status="rendered")
    assert daily.run(conn, settings) == {"space": "made 0, scheduled 1"} and len(made) == 1
    st = daily.status(settings)
    assert st["channels"] == ["space"]


def test_daily_task_outside_windows(settings, client, monkeypatch):
    monkeypatch.setattr(daily.os, "name", "posix")
    assert "Windows Task Scheduler" in daily.set_task(settings, True, "08:30")
    with pytest.raises(ValueError):
        daily.set_task(settings, True, "8.30")
    assert client.post("/schedule/daily", data={"on": "1", "time": "8.30"}).status_code == 400


def test_stats_page(client, conn):
    a = add_short(conn, status="scheduled", title="Big one", youtube_id="y1", publish_at="2026-10-01T15:00:00Z")
    b = add_short(conn, status="scheduled", title="Small one", youtube_id="y2", publish_at="2026-10-02T15:00:00Z")
    conn.executemany("INSERT INTO stats (short_id, views, engaged_views, avg_view_pct, likes, subs_gained) VALUES (?,?,?,?,?,?)",
                     [(a, 100, 50, 60.0, 1, 0), (a, 5000, 3000, 70.0, 40, 5), (b, 300, 60, 40.0, 2, 1)])
    conn.commit()
    page = client.get("/stats").data.decode()
    assert "5,000" in page and "100</td>" not in page  # only the latest fetch per Short counts
    assert page.index("Big one") < page.index("Small one")
    page = client.get("/stats?sort=newest").data.decode()
    assert page.index("Small one") < page.index("Big one")
    assert "Fetch stats" in page and "Per format" in page


class _FakeYouTube:
    """videos().insert(...).next_chunk(): ok `ok` times, then YouTube's uploadLimitExceeded."""

    def __init__(self, ok):
        self.ok, self.calls = ok, 0

    def videos(self):
        return self

    def insert(self, **_):
        return self

    def next_chunk(self):
        from googleapiclient.errors import ResumableUploadError
        self.calls += 1
        if self.calls > self.ok:
            content = json.dumps({"error": {"errors": [{"reason": "uploadLimitExceeded",
                                                        "domain": "youtube.video"}]}}).encode()
            raise ResumableUploadError(type("Resp", (), {"status": 400, "reason": "Bad Request"})(), content)
        return None, {"id": f"yt{self.calls}"}


@pytest.fixture
def fake_upload(monkeypatch):
    import googleapiclient.http
    from shorts import render
    monkeypatch.setattr(googleapiclient.http, "MediaFileUpload", lambda *a, **k: None)
    monkeypatch.setattr(render, "metadata", lambda c, ch, sid: {"title": "T", "description": "D"})
    monkeypatch.setattr(upload, "check_channel", lambda yt, ch: None)

    def use(yt):
        monkeypatch.setattr(upload, "youtube", lambda s, ch: yt)
        return yt
    return use


def test_upload_limit_stops_cleanly(conn, settings, channel, fake_upload):
    fake_upload(_FakeYouTube(ok=2))
    ids = [add_short(conn, video_path="x.mp4") for _ in range(4)]
    assert upload.upload_approved(conn, settings, channel) == ["yt1", "yt2"]
    status = [r[0] for r in conn.execute(f"SELECT status FROM shorts WHERE id IN ({','.join('?' * 4)}) ORDER BY id", ids)]
    assert status == ["scheduled", "scheduled", "approved", "approved"]
    # Blocked for 24 hours: the next run doesn't even try.
    yt = fake_upload(_FakeYouTube(ok=10))
    assert upload.upload_approved(conn, settings, channel) == [] and yt.calls == 0
    assert upload.blocked_until(settings, channel)


def test_channel_daily_cap(conn, settings, channel, fake_upload):
    fake_upload(_FakeYouTube(ok=10))
    settings.channel_uploads_per_day = 3
    add_short(conn, status="scheduled", youtube_id="old", uploaded_at=upload.iso(datetime.now(timezone.utc)))
    add_short(conn, status="scheduled", youtube_id="older", channel="other",
              uploaded_at=upload.iso(datetime.now(timezone.utc)))  # other channels don't count
    for _ in range(5):
        add_short(conn, video_path="x.mp4")
    assert len(upload.upload_approved(conn, settings, channel)) == 2
    assert conn.execute("SELECT COUNT(*) FROM shorts WHERE status='approved'").fetchone()[0] == 3
    assert upload.upload_approved(conn, settings, channel) == []
