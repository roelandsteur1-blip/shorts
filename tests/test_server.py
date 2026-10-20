import os

from shorts import server


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
