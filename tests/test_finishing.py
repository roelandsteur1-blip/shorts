"""finishing-up.md: channels compared at the same age, a fair voice test, learning from winners and
reject reasons, warnings on Home."""
import json
from datetime import date

import pytest

from shorts import upload
from shorts.web import stats as stats_page

from test_schedule_stats import add_short, cfg, client  # noqa: F401 - fixtures


class _FakeAnalytics:
    """youtubeAnalytics reports().query(...).execute(): views = days in the period x 100."""

    def __init__(self):
        self.queries = []

    def reports(self):
        return self

    def query(self, **kw):
        self.queries.append(kw)
        self.kw = kw
        return self

    def execute(self):
        days = (date.fromisoformat(self.kw["endDate"]) - date.fromisoformat(self.kw["startDate"])).days + 1
        return {"rows": [[days * 100, days * 50, 60.0, 20.0, days, 0, days]]}


@pytest.fixture
def analytics(monkeypatch):
    import googleapiclient.discovery
    fake = _FakeAnalytics()
    monkeypatch.setattr(upload, "credentials", lambda *a, **k: None)
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **k: fake)
    return fake


def test_stats_at_age(conn, settings, channel, analytics):
    old = add_short(conn, status="scheduled", youtube_id="y1", publish_at="2026-10-01T15:00:00Z")
    new = add_short(conn, status="scheduled", youtube_id="y2", publish_at="2026-10-12T15:00:00Z")
    assert upload.fetch_stats(conn, settings, channel, today=date(2026, 10, 17)) == 2
    ages = {(r[0], r[1]): r[2] for r in conn.execute("SELECT short_id, days, views FROM stats_age")}
    # The old Short gets all three; the new one only day 1 and day 3 (ends 14 Oct, final from 17 Oct); day 7 is not final yet.
    assert ages == {(old, 1): 100, (old, 3): 300, (old, 7): 700, (new, 1): 100, (new, 3): 300}
    n = len(analytics.queries)
    upload.fetch_stats(conn, settings, channel, today=date(2026, 10, 17))
    assert len(analytics.queries) == n + 2  # only the two lifetime queries again
    assert conn.execute("SELECT COUNT(*) FROM stats").fetchone()[0] == 4


def _age(conn, sid, days, views, engaged=0, pct=50.0, subs=0):
    conn.execute("INSERT INTO stats_age (short_id, days, views, engaged_views, avg_view_pct, subs_gained) VALUES (?,?,?,?,?,?)",
                 (sid, days, views, engaged, pct, subs))
    conn.commit()


def test_compare_channels(conn):
    for i, v in enumerate([100, 200, 900]):  # this week (reached 1 day between 8 and 14 Oct)
        _age(conn, add_short(conn, status="scheduled", publish_at=f"2026-10-1{i}T15:00:00Z"), 1, v, v // 2, 60.0, 1)
    _age(conn, add_short(conn, status="scheduled", publish_at="2026-10-03T15:00:00Z"), 1, 50, 10, 40.0, 0)  # last week
    _age(conn, add_short(conn, channel="other", status="scheduled", publish_at="2026-10-03T15:00:00Z"), 1, 5000, 100)
    rows = stats_page.compare_channels(conn, 1, today=date(2026, 10, 17))
    assert [r["channel"] for r in rows] == ["other", "space"]
    space = rows[1]
    assert space["shorts"] == 4 and space["views"] == 150  # median, not the 900 outlier
    assert space["change"]["views"] == 200 - 50 and round(space["change"]["viewed"]) == 20
    assert rows[0]["change"] == {}  # nothing this week to compare
    assert [r["channel"] for r in stats_page.compare_channels(conn, 1, "engaged", today=date(2026, 10, 17))][0] == "space"


def test_stats_page_compare_and_fetch_all(client, conn):
    _age(conn, add_short(conn, status="scheduled", youtube_id="y1", publish_at="2026-10-01T15:00:00Z"), 3, 1234, 600)
    page = client.get("/stats?days=3").data.decode()
    assert "Channels compared at the same age" in page and "1,234" in page and "Fetch stats for all channels" in page
    assert "1,234" not in client.get("/stats?days=7").data.decode()
    r = client.post("/stats/fetch-all")
    job = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert json.loads(job["command"]) == ["stats", "--all"] and job["heavy"] == 0
    assert r.headers["Location"].endswith(f"/jobs/{job['id']}")


def test_stats_all_skips_channels_without_login(conn, settings, cfg, monkeypatch):  # noqa: F811
    (settings.secrets_dir / "token-space.json").write_text("{}")
    calls = []
    monkeypatch.setattr(upload, "fetch_stats", lambda c, s, ch, interactive=True: calls.append((ch.id, interactive)) or 3)
    assert upload.fetch_stats_all(conn, settings) == {"space": "stats for 3"}
    assert calls == [("space", False)]


def test_expired_login_never_opens_browser(settings, channel):
    with pytest.raises(upload.LoginExpired):
        upload.credentials(settings, channel, interactive=False)
