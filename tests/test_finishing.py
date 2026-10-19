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

    def videos(self):  # youtube v3 videos().list(...): live counts
        return self

    def list(self, **kw):
        self.kw = kw
        return self

    def execute(self):
        if "id" in self.kw:
            return {"items": [{"id": v, "statistics": {"viewCount": "1234", "likeCount": "5"}}
                              for v in self.kw["id"].split(",")]}
        if self.kw["filters"] in getattr(self, "no_rows", ()):
            return {}
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


def test_voice_test_scores_against_own_channel(conn, client):
    # A big channel, all voiced (says nothing), and a small one with both kinds: voice does 2x there.
    for v in (10000, 12000):
        _age(conn, add_short(conn, channel="big", voice="af_heart", status="scheduled", publish_at="2026-10-01"), 3, v)
    for v, voice in [(100, None), (100, None), (200, "af_heart"), (200, "af_heart")]:
        _age(conn, add_short(conn, voice=voice, status="scheduled", publish_at="2026-10-01"), 3, v)
    res = stats_page.voice_test(conn, 3)
    assert res["one_kind"] == 1 and [r["channel"] for r in res["channels"]] == ["space"]
    g = {x["voiced"]: x for x in res["groups"]}
    assert g[True]["shorts"] == 2 and g[True]["views"] > g[False]["views"]
    assert round(g[True]["views"] / g[False]["views"], 1) == 2.0
    page = client.get("/stats?days=3").data.decode()
    assert "Views vs own channel" in page and "1 channel with only one kind left out" in page


def test_script_prompt_learns_from_winners_and_rejects(conn, channel):
    from shorts import script
    assert script.winners_note(conn, "space") == "" and script.rejects_note(conn, "space") == ""
    for v in range(1, 9):
        _age(conn, add_short(conn, status="scheduled", hook=f"hook {v}", title=f"title {v}"), 3, v * 100)
    _age(conn, add_short(conn, status="scheduled", hook="other channel"), 3, 99999)
    conn.execute("UPDATE shorts SET channel='other' WHERE hook='other channel'")
    add_short(conn, status="rejected", hook="Did you know rocks", reject_reason="weak hook: too vague")
    add_short(conn, status="rejected", hook="pretty clip", reject_reason="bad footage")  # not about the writing
    lessons = script.winners_note(conn, "space") + script.rejects_note(conn, "space")
    assert "first 3 days" in lessons and "800 views. Hook: \"hook 8\"" in lessons and "100 views" in lessons
    assert lessons.index("hook 8") < lessons.index("Worst") < lessons.index("hook 1")
    assert "hook 5" not in lessons and "other channel" not in lessons
    assert "Reason: weak hook: too vague" in lessons and "pretty clip" not in lessons
    prompt = script.build_prompt(channel, {"title": "T", "brief": "b"}, [], [], lessons)
    assert "Avoid the same mistakes" in prompt and prompt.index("Worst") < prompt.index("Topics we already covered")


def test_reject_rate_on_stats(client, conn):
    add_short(conn, status="approved")
    add_short(conn, status="rejected", reject_reason="weak hook: meh")
    add_short(conn, status="rejected", reject_reason="weak hook")
    add_short(conn, status="rendered")  # not reviewed yet
    rates = stats_page.review_rates(conn)
    assert rates == [{"channel": "space", "reviewed": 3, "rejected": 2, "rate": 2 / 3, "reasons": [("weak hook", 2)]}]
    assert "weak hook (2)" in client.get("/stats").data.decode()


def test_home_warnings(client, conn, settings):
    from datetime import datetime, timedelta, timezone
    page = client.get("/").data.decode()
    assert "Needs attention" not in page  # tests: restock_below 0, no logins
    settings.restock_below = 1000
    (settings.secrets_dir / "token-space.json").write_text("{}")
    upload.login_expired_path(settings, "space").write_text("2026-10-17T10:00:00Z")
    (settings.data_dir / "upload-blocked-space.txt").write_text(upload.iso(datetime.now(timezone.utc)))
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT15:00:00Z")
    add_short(conn, status="scheduled", youtube_id="y", publish_at=tomorrow)
    add_short(conn, status="approved")
    page = client.get("/").data.decode()
    assert "Needs attention" in page and "YouTube login expired" in page and "daily upload limit reached" in page
    assert "footage running low" in page and "1 approved waiting" in page
    empty = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%a %d %b")
    assert f"nothing scheduled on {empty}" in page
    conn.execute("DELETE FROM blocks"); conn.execute("DELETE FROM shorts"); conn.commit()
    assert "nothing scheduled in the next 7 days" in client.get("/").data.decode()


def test_stats_hidden_shorts(client, conn, settings, channel, analytics):
    """Old uploads kept as a record (their footage and topics stay used) are left out of Stats."""
    from shorts import db
    hidden = add_short(conn, status="scheduled", title="Old upload", youtube_id="y1", publish_at="2026-10-01T15:00:00Z")
    shown = add_short(conn, status="scheduled", title="New upload", youtube_id="y2", publish_at="2026-10-01T15:00:00Z")
    db.set_short(conn, hidden, stats_hidden=1)
    assert upload.fetch_stats(conn, settings, channel, today=date(2026, 10, 17)) == 1
    assert {r[0] for r in conn.execute("SELECT short_id FROM stats")} == {shown}
    _age(conn, hidden, 3, 9999)
    page = client.get("/stats?days=3").data.decode()
    assert "New upload" in page and "Old upload" not in page and "9,999" not in page


def test_stats_before_analytics(client, conn, settings, channel, analytics):
    """Analytics lags days: a new Short shows live views and '–' instead of 0 and 0%; Shorts that
    aren't public yet are skipped."""
    analytics.no_rows = {"video==y1"}
    add_short(conn, status="scheduled", title="Fresh", youtube_id="y1", publish_at="2026-10-17T00:00:00Z")
    add_short(conn, status="scheduled", title="Later", youtube_id="y2", publish_at="2999-01-01T15:00:00Z")
    assert upload.fetch_stats(conn, settings, channel) == 1
    row = conn.execute("SELECT views, engaged_views, avg_view_pct, live_views, live_likes FROM stats").fetchone()
    assert tuple(row) == (None, None, None, 1234, 5)
    page = client.get("/stats").data.decode()
    assert "Fresh" in page and "1,234" in page and ">0%<" not in page and "not in YouTube Analytics yet" in page
