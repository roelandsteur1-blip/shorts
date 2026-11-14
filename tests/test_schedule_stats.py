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




