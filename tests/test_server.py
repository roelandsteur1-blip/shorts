import os
import subprocess

from shorts import daily, server


def test_rebase_paths_rewrites_windows_paths(conn):
    conn.execute("INSERT INTO assets (source, source_id, license, path, status) VALUES "
                 "('nasa', '1', 'public-domain', 'D:\\shorts-footage\\nasa\\a.mp4', 'ok')")
    conn.execute("INSERT INTO assets (source, source_id, license, path, status) VALUES "
                 "('nasa', '2', 'public-domain', '/already/linux.mp4', 'ok')")
    aid = conn.execute("SELECT id FROM assets WHERE source_id='1'").fetchone()[0]
    conn.execute("INSERT INTO scenes (asset_id, start, end, keyframe) VALUES (?, 0, 1, ?)",
                 (aid, "d:/shorts-data/keyframes/1/0.jpg"))
    conn.commit()
    changed = server.rebase_paths(conn, [("D:\\shorts-footage", "/srv/footage"), ("D:\\shorts-data", "/srv/data")])
    assert changed["assets.path"] == 1 and changed["scenes.keyframe"] == 1
    paths = {r[0] for r in conn.execute("SELECT path FROM assets")}
    assert paths == {"/srv/footage/nasa/a.mp4", "/already/linux.mp4"}
    assert conn.execute("SELECT keyframe FROM scenes").fetchone()[0] == "/srv/data/keyframes/1/0.jpg"


def test_rebase_paths_needs_a_folder_boundary(conn):
    conn.execute("INSERT INTO assets (source, source_id, license, path, status) VALUES "
                 "('nasa', '1', 'public-domain', 'D:/shorts-data2/x.mp4', 'ok')")
    conn.commit()
    assert server.rebase_paths(conn, [("D:/shorts-data", "/srv/data")])["assets.path"] == 0


def test_backup_keeps_the_newest(conn, settings):
    folder = settings.data_dir / "backups"
    folder.mkdir()
    for i in range(9):
        (folder / f"shorts-2026010{i}-0000.db").write_bytes(b"")
    out = server.backup(conn, settings)
    assert out.exists() and out.stat().st_size > 0
    left = sorted(p.name for p in folder.glob("shorts-*.db"))
    assert len(left) == server.KEEP_BACKUPS and out.name in left


def test_load_env_does_not_override(settings, monkeypatch):
    monkeypatch.delenv("SHORTS_TEST_TOKEN", raising=False)
    monkeypatch.setenv("SHORTS_TEST_KEEP", "mine")
    (settings.secrets_dir / server.ENV_FILE).write_text(
        "# comment\nSHORTS_TEST_TOKEN='abc'\nSHORTS_TEST_KEEP=file\n", encoding="utf-8")
    server.load_env(settings)
    assert os.environ["SHORTS_TEST_TOKEN"] == "abc"
    assert os.environ["SHORTS_TEST_KEEP"] == "mine"
    monkeypatch.delenv("SHORTS_TEST_TOKEN")


def test_daily_task_uses_cron_on_linux(settings, monkeypatch):
    if os.name == "nt":
        return
    table = {"text": "5 1 * * * other job\n"}

    def fake_run(cmd, input=None, **kw):
        if cmd == ["crontab", "-l"]:
            return subprocess.CompletedProcess(cmd, 0, table["text"], "")
        table["text"] = input
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(daily.subprocess, "run", fake_run)
    assert "on at 07:30" in daily.set_task(settings, True, "07:30")
    lines = table["text"].splitlines()
    assert lines[0] == "5 1 * * * other job"
    assert lines[1].startswith("30 7 * * * ") and lines[1].endswith(daily.CRON_TAG) and " daily " in lines[1]
    assert daily.status(settings)["task"] is True
    daily.set_task(settings, True, "08:00")  # replaces, never doubles
    assert sum(l.endswith(daily.CRON_TAG) for l in table["text"].splitlines()) == 1
    daily.set_task(settings, False)
    assert table["text"] == "5 1 * * * other job\n"
    assert daily.status(settings)["task"] is False
