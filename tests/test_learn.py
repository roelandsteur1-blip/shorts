"""Learning from results: retention per block, length and title patterns, footage scores."""
import json

from shorts import learn


def short(conn, title, duration, engaged, curve=None, texts=("Hook here", "middle bit", "payoff")):
    cur = conn.execute("INSERT INTO shorts (channel, format_id, status, title, hook, duration) VALUES ('space','f','published',?,?,?)",
                       (title, texts[0], duration))
    sid = cur.lastrowid
    conn.execute("INSERT INTO stats_age (short_id, days, views, engaged_views) VALUES (?, 3, ?, ?)", (sid, engaged * 2, engaged))
    for i, t in enumerate(texts):
        conn.execute("INSERT INTO blocks (short_id, idx, text, seconds) VALUES (?,?,?,?)", (sid, i, t, duration / len(texts)))
    if curve:
        conn.execute("INSERT INTO retention (short_id, curve) VALUES (?, ?)", (sid, json.dumps(curve)))
    conn.commit()
    return sid


def test_patterns_note_length_and_titles(conn):
    for i in range(3):
        short(conn, f"Why is the sky {i}?", 25, 4000)   # short question titles do well
        short(conn, f"The sky story {i}", 50, 1000)
    note = learn.patterns_note(conn, "space")
    assert "under 30 s do best" in note
    assert "Titles with a question do +" in note


def test_retention_note_finds_the_block_that_loses_people(conn):
    # Viewers leave in the middle third: block 2 ("boring part") loses them fastest.
    curve = [[x / 10, 1.0 if x <= 3 else (0.4 if x >= 7 else 1.0 - 0.15 * (x - 3))] for x in range(11)]
    for i in range(6):
        short(conn, f"T{i}", 30, 1000 + i, curve, texts=(f"Hook {i}", f"boring part {i}", f"payoff {i}"))
    note = learn.retention_note(conn, "space")
    assert "after the hook a median 95%" in note
    worst = note.split("lost viewers fastest:")[1].split("held them best")[0]
    assert "boring part" in worst and "payoff" not in worst
    assert learn.retention_note(conn, "other") == ""
