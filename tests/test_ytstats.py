import json
"""Stats straight from YouTube (ytstats.py): every video on the channel, its state, live numbers,
Analytics once it has them, and the Stats page built from that."""
from datetime import date, datetime, timezone

import pytest

from shorts import db, upload, ytstats

from test_schedule_stats import add_short, cfg, client  # noqa: F401 - fixtures

NOW = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeYouTube:
    """Both clients: youtube v3 (channels, playlistItems, videos) and youtubeAnalytics v2 (reports).
    `videos` maps id -> (privacy, publishAt, views, duration); `playlist` is the uploads list (default:
    every video); `analytics` maps id -> its 7 metrics (missing = not in Analytics)."""

    def __init__(self):
        self.videos, self.playlist, self.analytics, self.calls = {}, None, {}, []
        self.channel_id, self.through, self.fail = "UCspace", "2026-10-17", None

    def add(self, vid, privacy="public", publish_at=None, views=0, duration="PT45S", title=None, status="processed"):
        self.videos[vid] = {"id": vid, "snippet": {"title": title or f"Video {vid}", "publishedAt": "2026-10-10T10:00:00Z"},
                            "status": {"privacyStatus": privacy, "uploadStatus": status,
                                       **({"publishAt": publish_at} if publish_at else {})},
                            "statistics": {"viewCount": str(views), "likeCount": str(views // 10), "commentCount": "1"},
                            "contentDetails": {"duration": duration}}

    def channels(self):
        return self

    def playlistItems(self):  # noqa: N802 - the API's name
        return self

    def reports(self):
        return self

    def list(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError(self.fail)
        if kw.get("mine"):
            return _Req(lambda: {"items": [{"id": self.channel_id, "contentDetails": {"relatedPlaylists": {"uploads": "UU"}}}]})
        if "playlistId" in kw:
            ids = list(self.videos) if self.playlist is None else self.playlist
            page = int(kw.get("pageToken") or 0)
            nxt = {"nextPageToken": str(page + 50)} if page + 50 < len(ids) else {}
            return _Req(lambda: {"items": [{"contentDetails": {"videoId": v}} for v in ids[page:page + 50]], **nxt})
        ids = kw["id"].split(",")
        assert len(ids) <= 50
        return _Req(lambda: {"items": [self.videos[v] for v in ids if v in self.videos]})

    def query(self, **kw):
        self.calls.append(kw)
        if kw.get("dimensions") == "day":
            return _Req(lambda: {"rows": [[self.through, 5]]})
        if kw.get("dimensions") == "elapsedVideoTimeRatio":
            return _Req(lambda: {"rows": [[k / 100, 1.0 - k / 200] for k in range(1, 101)]})
        if kw.get("dimensions") == "video":
            assert kw["sort"] == "-views" and kw["maxResults"] <= 200
            return _Req(lambda: {"rows": [[v, *m] for v, m in self.analytics.items()]})
        days = (date.fromisoformat(kw["endDate"]) - date.fromisoformat(kw["startDate"])).days + 1
        return _Req(lambda: {"rows": [[days * 100, days * 50, 60.0, 20.0, days, 0, days]]})


@pytest.fixture
def yt(monkeypatch):
    import googleapiclient.discovery
    fake = FakeYouTube()
    monkeypatch.setattr(upload, "credentials", lambda *a, **k: None)
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **k: _Client(fake))
    return fake


class _Client:
    """videos() on the fake has to be a method returning something with list(); keep names apart."""

    def __init__(self, fake):
        self.fake = fake

    def __getattr__(self, name):
        if name == "videos":
            return lambda: self.fake
        return getattr(self.fake, name)


def sync(conn, settings, channel, now=NOW):
    return ytstats.sync_channel(conn, settings, channel, now=now)


def states(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT youtube_id, state FROM yt_videos")}


def test_every_video_and_its_state(conn, settings, channel, yt):
    a = add_short(conn, status="scheduled", youtube_id="live1", publish_at="2026-10-17T15:00:00Z")
    add_short(conn, status="scheduled", youtube_id="later", publish_at="2026-10-19T21:00:00Z")
    add_short(conn, status="scheduled", youtube_id="deleted", publish_at="2026-10-18T15:00:00Z")
    yt.add("live1", views=500)
    yt.add("later", privacy="private", publish_at="2026-10-19T21:00:00Z")
    yt.add("byhand", views=40, title="Made by hand")          # not from the pipeline
    yt.add("unlisted", privacy="unlisted", views=3)
    yt.add("draft", privacy="private")                         # private, no schedule
    yt.add("long", duration="PT12M3S", views=99)              # not a Short
    yt.add("busy", status="uploaded")                          # still processing
    yt.playlist = ["live1", "byhand", "unlisted", "draft", "long", "busy"]  # scheduled one not in the playlist
    yt.analytics = {"live1": [450, 300, 65.0, 22.0, 40, 3, 2]}
    assert sync(conn, settings, channel) == 3                  # live1, byhand, unlisted
    assert states(conn) == {"live1": "live", "later": "scheduled", "byhand": "live", "unlisted": "live",
                            "draft": "private", "long": "live", "busy": "processing", "deleted": "gone"}
    v = conn.execute("SELECT * FROM yt_videos WHERE youtube_id='live1'").fetchone()
    assert (v["short_id"], v["views"], v["an_views"], v["engaged_views"], v["avg_view_pct"]) == (a, 500, 450, 300, 65.0)
    assert conn.execute("SELECT is_short FROM yt_videos WHERE youtube_id='long'").fetchone()[0] == 0
    assert conn.execute("SELECT short_id FROM yt_videos WHERE youtube_id='byhand'").fetchone()[0] is None
    assert conn.execute("SELECT status FROM shorts WHERE id=?", (a,)).fetchone()[0] == "published"
    # the Short that hasn't gone live has no numbers and no stats row
    assert conn.execute("SELECT views FROM yt_videos WHERE youtube_id='later'").fetchone()[0] is None
    assert {r[0] for r in conn.execute("SELECT short_id FROM stats")} == {a}
    sy = conn.execute("SELECT * FROM yt_sync").fetchone()
    assert sy["error"] is None and sy["analytics_through"] == "2026-10-17" and sy["yt_channel_id"] == "UCspace"


def test_not_in_analytics_yet_vs_zero(conn, settings, channel, yt):
    yt.add("new", views=1234, publish_at=None)
    yt.add("old", views=7)
    add_short(conn, status="scheduled", youtube_id="new", publish_at="2026-10-18T15:00:00Z")
    add_short(conn, status="scheduled", youtube_id="old", publish_at="2026-10-01T15:00:00Z")
    sync(conn, settings, channel)
    rows = {r[0]: tuple(r[1:]) for r in conn.execute("SELECT youtube_id, views, an_views, engaged_views FROM yt_videos")}
    assert rows == {"new": (1234, None, None), "old": (7, 0, 0)}  # young: not known yet; old: really 0


def test_paging_and_batches(conn, settings, channel, yt):
    for i in range(120):
        yt.add(f"v{i}", views=i)
    assert sync(conn, settings, channel) == 120
    assert all(len(c.get("id", "").split(",")) <= 50 for c in yt.calls if "id" in c)


def test_failure_keeps_last_numbers(conn, settings, channel, yt):
    yt.add("v1", views=10)
    sync(conn, settings, channel)
    yt.fail = "quotaExceeded"
    with pytest.raises(RuntimeError):
        sync(conn, settings, channel)
    assert conn.execute("SELECT views FROM yt_videos").fetchone()[0] == 10
    assert "quotaExceeded" in conn.execute("SELECT error FROM yt_sync").fetchone()[0]
    yt.fail = None
    sync(conn, settings, channel)
    assert conn.execute("SELECT error FROM yt_sync").fetchone()[0] is None


def test_login_for_another_channel(conn, settings, channel, yt):
    channel.youtube_channel_id = "UCother"
    yt.add("v1")
    with pytest.raises(RuntimeError, match="another channel"):
        sync(conn, settings, channel)
    assert conn.execute("SELECT COUNT(*) FROM yt_videos").fetchone()[0] == 0


def test_stats_at_age_once_final(conn, settings, channel, yt):
    old = add_short(conn, status="scheduled", youtube_id="y1", publish_at="2026-10-01T15:00:00Z")
    new = add_short(conn, status="scheduled", youtube_id="y2", publish_at="2026-10-12T15:00:00Z")
    yt.add("y1"); yt.add("y2")  # noqa: E702
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    sync(conn, settings, channel, now)
    ages = {(r[0], r[1]): r[2] for r in conn.execute("SELECT short_id, days, views FROM stats_age")}
    # The new Short's day 7 (ends 18 Oct) isn't final yet.
    assert ages == {(old, 1): 100, (old, 3): 300, (old, 7): 700, (new, 1): 100, (new, 3): 300}
    curves = dict(conn.execute("SELECT short_id, curve FROM retention").fetchall())
    assert set(curves) == {old, new} and json.loads(curves[old])[0] == [0.01, 0.995]
    n = sum("filters" in c for c in yt.calls)
    sync(conn, settings, channel, now)
    assert sum("filters" in c for c in yt.calls) == n  # nothing asked twice


def test_old_zero_rows_cleaned(conn, settings, channel, yt):
    sid = add_short(conn, status="scheduled", youtube_id="y1", publish_at="2026-10-19T12:00:00Z")
    conn.execute("INSERT INTO stats (short_id, fetched_at, views) VALUES (?, '2026-10-19 11:21:54', 0)", (sid,))
    conn.commit()
    yt.add("y1", views=5)
    sync(conn, settings, channel)
    assert [r[0] for r in conn.execute("SELECT fetched_at FROM stats WHERE short_id=?", (sid,))] != ["2026-10-19 11:21:54"]
    assert conn.execute("SELECT COUNT(*) FROM stats WHERE fetched_at='2026-10-19 11:21:54'").fetchone()[0] == 0


def test_hidden_marks_cleared_once(tmp_path):
    path = tmp_path / "old.db"
    c = db.connect(path)
    c.execute("DROP TABLE yt_videos")
    sid = add_short(c, status="scheduled", youtube_id="y1", publish_at="2026-10-01T15:00:00Z")
    c.execute("UPDATE shorts SET stats_hidden=1 WHERE id=?", (sid,))
    c.commit(); c.close()  # noqa: E702
    c = db.connect(path)  # first start with yt_videos: old hidden marks go
    assert c.execute("SELECT stats_hidden FROM shorts").fetchone()[0] == 0
    c.execute("UPDATE shorts SET stats_hidden=1"); c.commit(); c.close()  # noqa: E702
    assert db.connect(path).execute("SELECT stats_hidden FROM shorts").fetchone()[0] == 1  # later hides stay


def test_stats_page(client, conn, settings, channel, yt):
    add_short(conn, status="scheduled", title="Big one", youtube_id="y1", publish_at="2026-10-15T15:00:00Z")
    add_short(conn, status="scheduled", title="Small one", youtube_id="y2", publish_at="2026-10-16T15:00:00Z")
    add_short(conn, status="scheduled", title="Tonight", youtube_id="y3", publish_at="2026-10-19T21:00:00Z")
    yt.add("y1", views=5000, title="Big one"); yt.add("y2", views=300, title="Small one")  # noqa: E702
    yt.add("y3", privacy="private", publish_at="2026-10-19T21:00:00Z", title="Tonight")
    yt.add("y4", views=77, title="Made by hand")
    yt.analytics = {"y1": [4000, 3000, 70.0, 20.0, 40, 1, 5], "y2": [300, 60, 40.0, 10.0, 2, 0, 1]}
    sync(conn, settings, channel)
    page = client.get("/stats").data.decode()
    live = page[page.index("Live Shorts"):page.index("Not live")]
    assert "5,000" in live and "75%" in live and live.index("Big one") < live.index("Small one")
    assert "Made by hand" in live and "not from pipeline" in live and "Tonight" not in live
    assert "Tonight" in page[page.index("Not live"):] and "Scheduled" in page
    assert "3</b>live" in page and "1</b>scheduled" in page and "Analytics up to 2026-10-17" in page
    page = client.get("/stats?sort=newest").data.decode()
    assert page.index("Small one") < page.index("Big one") < page.index("Made by hand")
    # Hide, then show again
    client.post("/stats/hide/y1", data={"hidden": "1"})
    page = client.get("/stats").data.decode()
    assert "5,000" not in page[page.index("Live Shorts"):page.index("Hidden")] and "Show again" in page
    assert conn.execute("SELECT stats_hidden FROM shorts WHERE youtube_id='y1'").fetchone()[0] == 1
    client.post("/stats/hide/y1", data={"hidden": "0"})
    assert "Show again" not in client.get("/stats").data.decode()


def test_stats_page_shows_errors(client, conn, settings, channel, yt):
    yt.fail = "the YouTube login for 'Space' has expired"
    with pytest.raises(RuntimeError):
        sync(conn, settings, channel)
    page = client.get("/stats").data.decode()
    assert "Last fetch failed" in page and "has expired" in page
