"""Dashboard pages and the background job runner (with a fake command instead of the CLI)."""
import sys
import time

import pytest

from shorts import jobs


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








def test_jobs_survive_a_dashboard_restart(conn, settings):
    """A new Runner (dashboard restarted) adopts a job that is still running, follows its
    progress and reads its exit code; a job whose process is gone shows as interrupted."""
    script = "import time; print('PROGRESS 1/2 render', flush=True); time.sleep(1.5); print('PROGRESS 2/2 render')"
    first = jobs.Runner(settings, command=fake_command(script))
    a = jobs.submit(conn, "space", ["make", "-n", "2"])
    first.tick(conn)
    proc = first.procs[a][0]
    gone = jobs.submit(conn, "space", ["scenes"])
    conn.execute("UPDATE jobs SET status='running', pid=999999999, log_path=? WHERE id=?",
                 (str(settings.data_dir / "logs" / "nope.log"), gone))
    conn.commit()
    first.procs.clear()  # the old dashboard is gone; its job keeps running

    second = jobs.Runner(settings, command=fake_command("raise SystemExit(9)"))
    assert second.adopt(conn) == 1
    status = dict(conn.execute("SELECT id, status FROM jobs").fetchall())
    assert (status[a], status[gone]) == ("running", "interrupted")
    run_until_idle(second, conn)
    proc.wait(timeout=10)
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (a,)).fetchone()
    assert (j["status"], j["exit_code"], j["progress_done"]) == ("done", 0, 2)

    # Stop also works on an adopted job
    first.command = fake_command("import time; time.sleep(30)")
    b = jobs.submit(conn, "space", ["ingest"])
    first.tick(conn)
    first.procs.clear()
    third = jobs.Runner(settings)
    third.adopt(conn)
    third.stop(conn, b)
    run_until_idle(third, conn)
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (b,)).fetchone()[0] == "stopped"








