"""finishing-up.md: channels compared at the same age, a fair voice test, learning from winners and
reject reasons, warnings on Home."""
import json
from datetime import date

import pytest

from shorts import upload

from test_schedule_stats import add_short, cfg  # noqa: F401 - fixtures


def _age(conn, sid, days, views, engaged=0, pct=50.0, subs=0):
    conn.execute("INSERT INTO stats_age (short_id, days, views, engaged_views, avg_view_pct, subs_gained) VALUES (?,?,?,?,?,?)",
                 (sid, days, views, engaged, pct, subs))
    conn.commit()






def test_stats_all_skips_channels_without_login(conn, settings, cfg, monkeypatch):  # noqa: F811
    (settings.secrets_dir / "token-space.json").write_text("{}")
    calls = []
    monkeypatch.setattr(upload, "fetch_stats", lambda c, s, ch, interactive=True: calls.append((ch.id, interactive)) or 3)
    assert upload.fetch_stats_all(conn, settings) == {"space": "stats for 3"}
    assert calls == [("space", False)]


def test_expired_login_never_opens_browser(settings, channel):
    with pytest.raises(upload.LoginExpired):
        upload.credentials(settings, channel, interactive=False)




def test_script_prompt_learns_from_winners_and_rejects(conn, channel):
    from shorts import script
    assert script.winners_note(conn, "space") == "" and script.rejects_note(conn, "space") == ""
    for v in range(1, 9):
        _age(conn, add_short(conn, status="scheduled", hook=f"hook {v}", title=f"title {v}"), 3, 900 - v * 100, v * 10)
    _age(conn, add_short(conn, status="scheduled", hook="other channel"), 3, 99999)
    conn.execute("UPDATE shorts SET channel='other' WHERE hook='other channel'")
    add_short(conn, status="rejected", hook="Did you know rocks", reject_reason="weak hook: too vague")
    add_short(conn, status="rejected", hook="pretty clip", reject_reason="bad footage")  # not about the writing
    lessons = script.winners_note(conn, "space") + script.rejects_note(conn, "space")
    assert "first 3 days" in lessons and "80 engaged views (100 views). Hook: \"hook 8\"" in lessons and "10 engaged views (800 views)" in lessons
    assert lessons.index("hook 8") < lessons.index("Worst") < lessons.index("hook 1")
    assert "hook 5" not in lessons and "other channel" not in lessons
    assert "Reason: weak hook: too vague" in lessons and "pretty clip" not in lessons
    prompt = script.build_prompt(channel, {"title": "T", "brief": "b"}, [], [], lessons)
    assert "Avoid the same mistakes" in prompt and prompt.index("Worst") < prompt.index("Topics we already covered")




