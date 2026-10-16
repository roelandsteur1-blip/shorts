"""Dashboard pages and the background job runner (with a fake command instead of the CLI)."""
import sys
import time

import pytest

from shorts import jobs
from shorts.web import create_app


def fake_command(script):
    """Runner command that runs a small Python script instead of `python -m shorts`."""
    return lambda settings, job: [sys.executable, "-c", script]


def run_until_idle(runner, conn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        runner.tick(conn)
        if not conn.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running')").fetchone():
            return
        time.sleep(0.05)
    raise AssertionError("jobs did not finish")


def test_runner_progress_and_exit_codes(conn, settings):
    script = "print('PROGRESS 1/3 script'); print('PROGRESS 2/3 render', flush=True); print('bye')"
    runner = jobs.Runner(settings, command=fake_command(script))
    ok = jobs.submit(conn, "space", ["make", "-n", "3"])
    run_until_idle(runner, conn)
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (ok,)).fetchone()
    assert (j["status"], j["exit_code"]) == ("done", 0)
    assert (j["progress_done"], j["progress_total"], j["progress"]) == (2, 3, "render")
    assert "bye" in jobs.tail(j["log_path"])

    runner.command = fake_command("import sys; sys.exit(3)")
    bad = jobs.submit(conn, "space", ["make", "-n", "1"])
    run_until_idle(runner, conn)
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (bad,)).fetchone()
    assert (j["status"], j["exit_code"]) == ("failed", 3)
    assert jobs.follow_up(j) == ("Finish", ["finish"])


def test_one_heavy_job_at_a_time(conn, settings):
    runner = jobs.Runner(settings, command=fake_command("import time; time.sleep(0.5)"))
    a = jobs.submit(conn, "space", ["make", "-n", "1"])
    b = jobs.submit(conn, "space", ["scenes"])
    c = jobs.submit(conn, "space", ["connect"])  # light: starts right away
    runner.tick(conn)
    status = dict(conn.execute("SELECT id, status FROM jobs").fetchall())
    assert (status[a], status[b], status[c]) == ("running", "queued", "running")
    run_until_idle(runner, conn)
    assert {r[0] for r in conn.execute("SELECT status FROM jobs")} == {"done"}


def test_stop_and_recover(conn, settings):
    runner = jobs.Runner(settings, command=fake_command("import time; time.sleep(30)"))
    a = jobs.submit(conn, "space", ["ingest"])
    b = jobs.submit(conn, "space", ["scenes"])
    runner.tick(conn)
    runner.stop(conn, b)  # queued: stops without running
    runner.stop(conn, a)
    run_until_idle(runner, conn)
    status = dict(conn.execute("SELECT id, status FROM jobs").fetchall())
    assert status == {a: "stopped", b: "stopped"}
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (a,)).fetchone()
    assert jobs.follow_up(j) == ("Run again", ["ingest"])

    c = jobs.submit(conn, "space", ["scenes"])
    conn.execute("UPDATE jobs SET status='running' WHERE id=?", (c,))
    assert jobs.recover(conn) == 1
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (c,)).fetchone()[0] == "interrupted"


@pytest.fixture
def client(settings):
    return create_app(settings, start_runner=False).test_client()


def test_home_and_make_page(client, conn):
    page = client.get("/").data
    assert b"Space Shorts" in page and b"Make Shorts" in page and b"YouTube not connected" in page
    assert b"what_youre_seeing" in client.get("/make").data
    r = client.post("/make", data={"channel": "space", "count": "3", "format": "scale_of"})
    job = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert r.headers["Location"].endswith(f"/jobs/{job['id']}")
    assert (job["channel"], job["command"], job["status"]) == ("space", '["make", "-n", "3", "--format", "scale_of"]', "queued")
    assert client.post("/make", data={"channel": "space", "format": "nope"}).status_code == 400
    assert client.post("/make", data={"channel": "nope"}).status_code == 404
    assert b"queued" in client.get(f"/jobs/{job['id']}").data
    assert client.get(f"/jobs/{job['id']}.json").get_json()["status"] == "queued"
    client.post(f"/jobs/{job['id']}/stop")
    assert client.get(f"/jobs/{job['id']}.json").get_json()["status"] == "stopped"


def test_dashboard_listens_on_localhost_only(settings, monkeypatch):
    from flask import Flask
    from shorts import web
    seen = {}
    monkeypatch.setattr(Flask, "run", lambda self, **kw: seen.update(kw))
    monkeypatch.setattr(jobs.Runner, "start", lambda self: None)
    web.serve(settings, 5001)
    assert seen["host"] == "127.0.0.1"


def test_shutdown_interrupts_running_jobs(conn, settings):
    runner = jobs.Runner(settings, command=fake_command("import time; time.sleep(30)"))
    a = jobs.submit(conn, "space", ["make", "-n", "1"])
    runner.tick(conn)
    proc = runner.procs[a][0]
    runner.shutdown()
    assert proc.wait(timeout=10) is not None
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (a,)).fetchone()[0] == "interrupted"
