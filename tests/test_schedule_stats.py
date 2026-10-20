"""Phase 5 and 6: edit text and re-render, Schedule page and upload quota, Stats page."""
import json
import shutil
from datetime import datetime, timezone

import pytest

from shorts import script, upload
from shorts.config import ROOT, list_channels, load_channel


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
    monkeypatch.setattr(render, "metadata", lambda c, ch, sid, settings=None: {"title": "T", "description": "D"})
    monkeypatch.setattr(upload, "check_channel", lambda yt, ch: None)

    def use(yt):
        monkeypatch.setattr(upload, "youtube", lambda s, ch, interactive=True: yt)
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


def test_daily_upload_all_channels(conn, settings, cfg, client, fake_upload):
    fake_upload(_FakeYouTube(ok=100))
    settings.channel_uploads_per_day = 2
    for _ in range(3):
        add_short(conn, video_path="x.mp4")
    out = upload.upload_all(conn, settings)
    assert out["space"] == "uploaded 2" and set(out) == set(list_channels(cfg))
    page = client.get("/schedule").data.decode()
    assert "Daily upload" in page and "2 of 2</b> uploaded in the last 24 hours" in page
    r = client.post("/schedule/upload-all")
    job = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert json.loads(job["command"]) == ["upload-all"] and job["channel"] is None
    assert r.headers["Location"].endswith(f"/jobs/{job['id']}")


def test_home_daily_buttons(client, conn, cfg):
    page = client.get("/").data.decode()
    assert "Make for every channel" in page and "Daily upload" in page
    for _ in range(3):
        add_short(conn, status="approved")
    add_short(conn, status="rendered")
    add_short(conn, status="scheduled")  # uploaded already: doesn't count
    r = client.post("/make-all", data={"count": "500"})  # capped at 100
    assert r.status_code == 302
    # One job for every channel (Claude writes the next channel while the previous one renders).
    jobs = conn.execute("SELECT channel, command FROM jobs").fetchall()
    assert len(jobs) == 1 and jobs[0]["channel"] is None
    assert json.loads(jobs[0]["command"]) == ["make-all", "--to", "100"]
    from shorts.config import load_channel
    from shorts.pipeline import top_up_orders
    chans = [load_channel(c, cfg) for c in list_channels(cfg)]
    orders, _ = top_up_orders(conn, chans, 100)
    assert {ch.id: n for ch, n, _ in orders}["space"] == 96
    # Space already has 4 waiting: topping up to 3 skips it.
    orders, enough = top_up_orders(conn, chans, 3)
    assert "space" not in {ch.id for ch, _, _ in orders} and any("(4)" in e for e in enough)
