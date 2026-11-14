"""Dashboard speed: footage counts in one pass (cached until the data changes), the database set up
once per process, YouTube's schedule served from cache while it refreshes in the background."""
import dataclasses

from shorts import db, hero
from test_multi_short import add_short, add_video
from test_schedule_stats import cfg  # noqa: F401 - fixtures


def fit_all(conn, channel):
    conn.execute("""INSERT OR REPLACE INTO channel_fit (channel, asset_id, fits, niche, reason, method)
                    SELECT ?, id, 1, ?, '', 'manual' FROM assets""", (channel.id, channel.niche))
    conn.commit()


def sort_into(conn, scene_ids, category):
    conn.executemany("INSERT OR REPLACE INTO scene_categories (scene_id, category, confidence, method) VALUES (?, ?, 0.9, 'clip')",
                     [(s, category) for s in scene_ids])
    conn.commit()


def test_footage_summary_matches_shorts_left(conn, settings, channel):
    channel = dataclasses.replace(channel, categories=["space", "history"])
    a, b = channel.categories
    _, ids1 = add_video(conn, 5)
    _, ids2 = add_video(conn, 10)
    _, ids3 = add_video(conn, 1)
    sort_into(conn, ids1, a)
    sort_into(conn, ids2[:20], a)
    sort_into(conn, ids2[10:], b)
    fit_all(conn, channel)
    add_short(conn, ids2[0])

    def expected():
        left, videos = hero.shorts_left(conn, settings, channel.id, fits=channel)
        return {"left": left, "videos": videos,
                "categories": {c: hero.shorts_left(conn, settings, channel.id, [c], fits=channel)[0]
                               for c in channel.categories}}

    got = hero.footage_summary(conn, settings, channel)
    assert got == expected() and got["left"] > 0 and got["categories"][a] > 0 and got["categories"][b] > 0

    # A new Short changes the counts at once (no stale cache).
    add_short(conn, ids1[0])
    assert hero.footage_summary(conn, settings, channel) == expected()
    # A rejected Short gives its slot back; a video found off-niche drops out.
    conn.execute("UPDATE shorts SET status='rejected' WHERE hero_scene_id=?", (ids1[0],))
    conn.execute("UPDATE channel_fit SET fits=0, at=datetime('now', '+1 minute') WHERE asset_id=(SELECT asset_id FROM scenes WHERE id=?)",
                 (ids3[0],))
    conn.commit()
    assert hero.footage_summary(conn, settings, channel) == expected()


def test_connect_sets_up_schema_once(settings, monkeypatch):
    db.connect(settings.db_path).close()
    calls = []
    monkeypatch.setattr(db, "_migrate", lambda c: calls.append(1))
    c = db.connect(settings.db_path)
    assert calls == [] and c.execute("SELECT COUNT(*) FROM shorts").fetchone()[0] == 0
    c.close()
    # A database replaced on disk (restored backup) is set up again.
    settings.db_path.unlink()
    for extra in ("-wal", "-shm"):
        (settings.db_path.parent / (settings.db_path.name + extra)).unlink(missing_ok=True)
    monkeypatch.undo()
    c = db.connect(settings.db_path)
    assert c.execute("SELECT COUNT(*) FROM shorts").fetchone()[0] == 0
    c.close()






