"""Transcript-first batch of 20 and its upload to one channel, 1 per day (Roeland 2026-11-13)."""
import json
from datetime import datetime, timezone

import pytest

from meta import common, runner, tclips
from shorts import upload


def _made(name, source, score, kind="newsreel"):
    return {"short": name, "kind": kind, "source": source, "score": score, "lengths": {f"{name}.mp4": 20.0},
            "hook": "Hook", "context": "Kennedy at Rice, 1962.", "yt_title": f"Title {name}",
            "credit": "Universal Newsreel", "title_source": "Newsreel", "year": 1962,
            "link": "https://archive.org/details/x", "why": "why"}


def test_spread_keeps_one_film_off_consecutive_days():
    rows = [{"source": s} for s in "AAAB"]
    assert [r["source"] for r in tclips.spread(rows)] == ["A", "B", "A", "A"]
    rows = [{"source": s} for s in "AABBC"]
    out = [r["source"] for r in tclips.spread(rows)]
    assert all(a != b for a, b in zip(out, out[1:]))


def test_batch_makes_every_kind_and_keeps_the_best(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(tclips, "OUT", out)
    monkeypatch.setattr(tclips, "UPLOAD_DIR", out / "upload")
    monkeypatch.setattr(tclips, "PLAN", out / "upload-plan.json")
    for kind in ("newsreel", "nasa"):
        (out / f"sources-{kind}.json").write_text("[]")
    calls = []

    def fake_make(kind, per_source, bare):
        calls.append((kind, per_source, bare))
        made = json.loads((out / "made.json").read_text()) if (out / "made.json").exists() else []
        for i in range(3):
            name = f"{kind}-{i}"
            (out / f"{name}.mp4").write_bytes(b"x")
            made.append(_made(name, f"{kind}-film", score=i + (10 if kind == "nasa" else 0), kind=kind))
        made.append({"kind": kind, "source": "bad", "failed": True})
        (out / "made.json").write_text(json.dumps(made))

    monkeypatch.setattr(tclips, "make", fake_make)
    plan = tclips.batch(4)
    assert calls == [("newsreel", 3, 0), ("nasa", 3, 0)]
    assert len(plan) == 4 and all(p["path"].startswith(str(out / "upload")) for p in plan)
    assert {p["short"] for p in plan} == {"nasa-0", "nasa-1", "nasa-2", "newsreel-2"}
    assert plan[0]["short"] == "nasa-2" and plan[1]["short"] == "newsreel-2"  # best first, films alternate
    assert json.loads((out / "upload-plan.json").read_text())[0]["path"] == plan[0]["path"]
    assert not (out / "nasa-2.mp4").exists() and (out / "newsreel-0.mp4").exists()


class _FakeYouTube:
    def __init__(self, ok=99):
        self.ok, self.calls, self.bodies = ok, 0, []

    def videos(self):
        return self

    def insert(self, body=None, **_):
        self.bodies.append(body)
        return self

    def next_chunk(self):
        from googleapiclient.errors import ResumableUploadError
        self.calls += 1
        if self.calls > self.ok:
            content = json.dumps({"error": {"errors": [{"reason": "uploadLimitExceeded"}]}}).encode()
            raise ResumableUploadError(type("Resp", (), {"status": 400, "reason": "Bad Request"})(), content)
        return None, {"id": f"yt{self.calls}"}


@pytest.fixture
def plan(tmp_path, monkeypatch, conn, settings, channel):
    import googleapiclient.http
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(tclips, "OUT", out)
    monkeypatch.setattr(tclips, "PLAN", out / "upload-plan.json")
    rows = []
    for i in range(7):
        p = out / f"s{i}.mp4"
        p.write_bytes(b"x")
        rows.append({**_made(f"s{i}", f"film{i % 3}", 1, kind="nasa" if i == 0 else "newsreel"), "path": str(p)})
    (out / "upload-plan.json").write_text(json.dumps(rows))
    monkeypatch.setattr(common, "open_library", lambda: (settings, conn, None))
    monkeypatch.setattr("shorts.config.load_channel", lambda cid, d=None: channel)
    monkeypatch.setattr(googleapiclient.http, "MediaFileUpload", lambda *a, **k: None)
    monkeypatch.setattr(upload, "check_channel", lambda yt, ch: None)

    def use(yt):
        monkeypatch.setattr(upload, "youtube", lambda s, ch, interactive=True: yt)
        return yt
    return use


def test_upload_schedules_one_per_day_and_resumes(plan, conn, settings, channel):
    settings.channel_uploads_per_day = 5
    yt = plan(_FakeYouTube())
    assert tclips.upload(channel.id) == [f"yt{i}" for i in range(1, 6)]
    rows = conn.execute("SELECT * FROM shorts WHERE format_id='tclips' ORDER BY publish_at").fetchall()
    days = [datetime.fromisoformat(r["publish_at"].replace("Z", "+00:00")).date() for r in rows]
    assert len(rows) == 5 and all(r["status"] == "scheduled" for r in rows)
    assert all((b - a).days == 1 for a, b in zip(days, days[1:]))
    assert days[0] > datetime.now(timezone.utc).date()
    body = yt.bodies[0]
    assert body["status"]["privacyStatus"] == "private" and body["status"]["publishAt"] == rows[0]["publish_at"]
    assert body["snippet"]["title"] == "Title s0" and "NASA does not endorse" in body["snippet"]["description"]
    assert "Universal Newsreel (1962)" in body["snippet"]["description"]
    # The normal daily upload never takes these: none is 'approved'.
    assert conn.execute("SELECT COUNT(*) FROM shorts WHERE status='approved'").fetchone()[0] == 0
    # Channel cap reached: nothing now; a day later the other two follow on the next days.
    assert tclips.upload(channel.id) == []
    conn.execute("UPDATE shorts SET uploaded_at='2000-01-01T00:00:00Z'")
    conn.commit()
    assert tclips.upload(channel.id) == ["yt6", "yt7"]
    days = [datetime.fromisoformat(r[0].replace("Z", "+00:00")).date() for r in conn.execute(
        "SELECT publish_at FROM shorts WHERE format_id='tclips' ORDER BY publish_at")]
    assert len(days) == 7 and all((b - a).days == 1 for a, b in zip(days, days[1:]))
    assert tclips.upload(channel.id) == []


def test_upload_limit_marks_failed_and_retries_that_short(plan, conn, settings, channel):
    plan(_FakeYouTube(ok=2))
    assert tclips.upload(channel.id) == ["yt1", "yt2"]
    assert [r[0] for r in conn.execute("SELECT status FROM shorts WHERE format_id='tclips' ORDER BY id")] == \
        ["scheduled", "scheduled", "failed"]
    assert upload.blocked_until(settings, channel)


def test_next_day_skips_a_taken_time(conn, channel):
    from datetime import timedelta
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    first = sorted(channel.publish_times_utc)[0]
    conn.execute("INSERT INTO shorts (channel, format_id, status, publish_at) VALUES (?, 'x', 'scheduled', ?)",
                 (channel.id, f"2026-11-14T{first}:00Z"))
    slot = tclips.next_day(conn, channel, now=now)
    assert slot.date().isoformat() == "2026-11-14" and upload.iso(slot) == upload.iso(
        datetime.fromisoformat(f"2026-11-14T{first}:00+00:00") + timedelta(hours=1))


def test_runner_allows_batch_and_upload():
    assert runner.ALLOWLIST["tclipsbatch"][1] == ["batch"]
    assert runner.ALLOWLIST["tclipsupload"][1] == ["upload"]


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _FakeChannel:
    """Uploads: v1 public, v2 scheduled, v3 private, v4 = one of ours (transcript-first)."""

    def __init__(self):
        self.deleted = []
        self.status = {"v1": ("public", None), "v2": ("private", "2026-11-20T15:00:00Z"),
                       "v3": ("private", None), "v4": ("private", "2026-11-14T15:00:00Z")}

    def channels(self):
        return self

    def playlistItems(self):
        return self

    def videos(self):
        return self

    def list(self, part=None, id=None, **kw):
        if "mine" in kw:
            return _Exec({"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU"}}}]})
        if "playlistId" in kw:
            return _Exec({"items": [{"contentDetails": {"videoId": v}} for v in self.status]})
        return _Exec({"items": [{"id": v, "snippet": {"title": f"T{v}"},
                                 "status": {"privacyStatus": p, **({"publishAt": t} if t else {})}}
                                for v, (p, t) in self.status.items() if v in id.split(",")]})

    def delete(self, id=None):
        self.deleted.append(id)
        return _Exec(None)


def test_clear_lists_then_deletes_only_unpublished_old_shorts(plan, conn, settings, channel, monkeypatch):
    from tests.test_schedule_stats import add_short
    yt = plan(_FakeChannel())
    add_short(conn, status="scheduled", youtube_id="v2", channel=channel.id)
    keep = add_short(conn, status="approved", channel=channel.id)
    conn.execute("INSERT INTO shorts (channel, format_id, status, youtube_id) VALUES (?, 'tclips', 'scheduled', 'v4')",
                 (channel.id,))
    conn.commit()
    assert [v["id"] for v in tclips.clear(channel.id)] == ["v3", "v2"] and yt.deleted == []
    assert json.loads((tclips.OUT / "clear-list.json").read_text())["approved"][0]["id"] == keep
    monkeypatch.setattr(upload, "can_manage", lambda s, ch: False)
    assert tclips.clear(channel.id, delete=True) == [] and yt.deleted == []
    monkeypatch.setattr(upload, "can_manage", lambda s, ch: True)
    tclips.clear(channel.id, delete=True)
    assert sorted(yt.deleted) == ["v2", "v3"]
    st = dict(conn.execute("SELECT youtube_id, status FROM shorts WHERE youtube_id IS NOT NULL").fetchall())
    assert st == {"v2": "rejected", "v4": "scheduled"}
    assert conn.execute("SELECT status FROM shorts WHERE id=?", (keep,)).fetchone()[0] == "rejected"
