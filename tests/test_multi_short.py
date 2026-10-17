"""Long videos give more than one Short: budget per video, hero spacing, counts, cleanup,
sibling-aware prompts and scheduling."""
from datetime import datetime, timezone

import numpy as np

from shorts import factcheck, hero, library as lib, script, upload


def vec(seed: int) -> bytes:
    v = np.random.default_rng(seed).standard_normal(32).astype(np.float32)
    return (v / np.linalg.norm(v)).tobytes()


def add_video(conn, minutes: float, scene_every: float = 10.0, title: str = "Long broadcast",
              path: str | None = None) -> tuple[int, list[int]]:
    """A scened video with one 6 s scene every scene_every seconds, each looking different."""
    n = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    aid = conn.execute(
        """INSERT INTO assets (source, source_id, title, description, license, duration, status, path)
           VALUES ('nasa', ?, ?, 'desc', 'public-domain', ?, 'scened', ?)""",
        (f"v{n}", title, minutes * 60, path)).lastrowid
    ids = []
    t = 0.0
    while t + 6 <= minutes * 60:
        ids.append(conn.execute(
            """INSERT INTO scenes (asset_id, start, end, motion, contrast, embedding)
               VALUES (?, ?, ?, 0.03, 0.2, ?)""", (aid, t, t + 6, vec(aid * 10000 + int(t)))).lastrowid)
        t += scene_every
    conn.commit()
    return aid, ids


def add_short(conn, scene_id: int, status: str = "rendered", publish_at: str | None = None,
              topic: str = "t") -> int:
    sid = conn.execute(
        """INSERT INTO shorts (channel, format_id, hero_scene_id, topic, hook, status, publish_at)
           VALUES ('space', 'f', ?, ?, 'hook', ?, ?)""", (scene_id, topic, status, publish_at)).lastrowid
    conn.execute("INSERT INTO blocks (short_id, idx, text, seconds, scene_id) VALUES (?, 0, 'hook text', 3, ?)",
                 (sid, scene_id))
    conn.commit()
    return sid


def start_of(conn, scene_id):
    return conn.execute("SELECT start FROM scenes WHERE id=?", (scene_id,)).fetchone()[0]


def test_budget(settings):
    assert hero.budget(settings, 60) == 1          # short clip: one Short, as before
    assert hero.budget(settings, None) == 1
    assert hero.budget(settings, 10 * 60) == 5     # 10 min / 90 s = 6, capped at 5
    assert hero.budget(settings, 4 * 60) == 2


def test_long_video_gives_several_heroes_far_apart(conn, settings):
    aid, _ = add_video(conn, 10)
    made = []
    for _ in range(7):
        picked = hero.candidates(conn, settings, "space", 5, use_clip=False)
        assert len(picked) <= 1  # one video: at most one hero per batch
        if not picked:
            break
        made.append(picked[0])
        add_short(conn, picked[0])
    assert len(made) == 5
    starts = sorted(start_of(conn, s) for s in made)
    assert all(b - a >= settings.segment_seconds for a, b in zip(starts, starts[1:]))


def test_counts_and_rejected_short_gives_slot_back(conn, settings, channel):
    add_video(conn, 1)          # 1 Short
    aid, ids = add_video(conn, 5)  # 300 s / 90 = 3 Shorts
    assert hero.shorts_left(conn, settings, "space") == (4, 2)
    sid = add_short(conn, ids[0])
    assert hero.shorts_left(conn, settings, "space") == (3, 2)
    conn.execute("UPDATE shorts SET status='rejected' WHERE id=?", (sid,))
    conn.commit()
    assert hero.shorts_left(conn, settings, "space") == (4, 2)
    rows = {v["asset_id"]: v for v in hero.per_video(conn, settings, "space")}
    assert rows[aid]["left"] == 3 and rows[aid]["budget"] == 3


def test_look_alike_hero_from_same_video_is_skipped(conn, settings):
    aid, ids = add_video(conn, 10)
    add_short(conn, ids[0])
    # Make a far-away scene look exactly like the first hero.
    far = [i for i in ids if start_of(conn, i) >= 200][0]
    conn.execute("UPDATE scenes SET embedding=(SELECT embedding FROM scenes WHERE id=?) WHERE id=?", (ids[0], far))
    conn.commit()
    eligible = {r["id"] for r in hero._eligible(conn, settings, "space", True, None)}
    assert far not in eligible and eligible


def test_cleanup_when_budget_used(conn, settings, tmp_path):
    f = tmp_path / "long.mp4"
    f.write_bytes(b"x")
    aid, ids = add_video(conn, 3, path=str(f))  # budget 2
    first = add_short(conn, ids[0], status="uploaded")
    assert lib.cleanup_candidates(conn, settings) == []  # one more Short to come
    second = [i for i in ids if start_of(conn, i) >= 90][0]
    add_short(conn, second, status="rendered")
    assert lib.cleanup_candidates(conn, settings) == []  # waiting for review
    conn.execute("UPDATE shorts SET status='scheduled'")
    conn.commit()
    [(a, why)] = lib.cleanup_candidates(conn, settings)
    assert a["id"] == aid and "2 Shorts" in why
    assert first


def test_script_and_factcheck_see_siblings(conn, settings, channel):
    aid, ids = add_video(conn, 15)
    add_short(conn, ids[0], status="scheduled", topic="the launch pad")
    ctx = script.hero_context(conn, ids[-1])
    prompt = script.build_prompt(channel, {"title": "T", "brief": "b"}, [ctx], [])
    assert "already gave other Shorts" in prompt and "the launch pad" in prompt
    sid = add_short(conn, ids[-1])
    item = factcheck._item(conn, sid)
    assert "Other Shorts from the same video: hook text" in item
    assert "15-minute video" in item


def test_siblings_are_scheduled_days_apart(conn, settings, channel):
    _, a_ids = add_video(conn, 10)
    _, b_ids = add_video(conn, 1, title="Other")
    s1 = add_short(conn, a_ids[0], status="approved")
    s2 = add_short(conn, a_ids[20], status="approved")
    s3 = add_short(conn, b_ids[0], status="approved")
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    slots = upload.slots_for(conn, settings, channel, [s1, s2, s3], now=now)
    assert [upload.iso(s) for s in slots] == ["2026-10-16T15:00:00Z", "2026-10-18T15:00:00Z",
                                              "2026-10-16T21:00:00Z"]
