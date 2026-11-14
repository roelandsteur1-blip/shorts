"""The transcript-first dashboard (shorts/web/tfirst.py) on files like the maker and runner leave."""
import json

import pytest

from shorts.web import create_app


@pytest.fixture
def out(tmp_path):
    base = tmp_path / "meta-out" / "tclips"
    (base / "examples").mkdir(parents=True)
    (base / "upload" / "2026-11-13").mkdir(parents=True)
    (tmp_path / "meta-out" / "transcripts").mkdir()
    made = [{"short": "newsreel-1a", "kind": "newsreel", "source": "UN-1962", "title_source": "Universal Newsreel 1962",
             "link": "https://archive.org/details/UN-1962", "year": 1962, "hook": "1962 promised Apollo by 1964",
             "context": "Kennedy at Huntsville", "payoff": "It took until 1968", "rights": "NARA, no restrictions",
             "lengths": {"newsreel-1a.mp4": 35.5}, "score": 4.0},
            {"short": "nasa-2b", "kind": "nasa", "source": "nasa:x", "title_source": "NASA film", "hook": "Never fly",
             "yt_title": "Doctors Said Never Fly", "lengths": {"nasa-2b.mp4": 30.0}}]
    (base / "made.json").write_text(json.dumps(made))
    (base / "examples.json").write_text(json.dumps([{"short": "ex-prelinger-2a", "kind": "prelinger", "hook": "Bacon class"}]))
    plan_path = base / "upload" / "2026-11-13" / "nasa-2b.mp4"
    plan_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)
    (base / "upload-plan.json").write_text(json.dumps([dict(made[1], path=str(plan_path))]))
    (base / "newsreel-1a.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"1" * 100)
    (base / "sources-newsreel.json").write_text(json.dumps([{"id": "UN-1962", "kind": "newsreel", "title": "Universal Newsreel 1962",
                                                             "length": 367, "link": "https://archive.org/details/UN-1962"}]))
    (tmp_path / "meta-out" / "transcripts" / "UN-1962.json").write_text("{}")
    (tmp_path / "meta-out" / "runner-done.json").write_text(json.dumps({"2026-11-13-tclips-1": "done", "2026-11-14-tclipsuploadall-2": "running"}))
    jobs = tmp_path / "jobs.toml"
    jobs.write_text('# old one\n[[job]]\nid = "2026-11-13-tclips-1"\nname = "tclips"\nn = 1\n\n'
                    '# the last 10 to historybuffed\n[[job]]\nid = "2026-11-14-tclipsuploadall-2"\nname = "tclipsuploadall"\n')
    return base, plan_path, jobs


@pytest.fixture
def client(settings, conn, out):
    base, plan_path, jobs = out
    conn.execute("""INSERT INTO shorts (channel, format_id, title, status, video_path, youtube_id, publish_at)
                    VALUES ('historybuffed', 'tclips', 'Doctors Said Never Fly', 'scheduled', ?, 'WpHmWh-47TY', '2099-11-22T15:00:00Z')""",
                 (str(plan_path),))
    conn.execute("INSERT INTO shorts (channel, format_id, title, status) VALUES ('space', 'classic', 'Old classic', 'approved')")
    conn.commit()
    app = create_app(settings, start_runner=False)
    app.config.update(TCLIPS_OUT=str(base), TCLIPS_JOBS=str(jobs))
    return app.test_client()


def test_overview_counts_and_jobs(client):
    html = client.get("/").get_data(as_text=True)
    assert "Transcript-first Shorts" in html
    assert "Doctors Said Never Fly" in html            # next on YouTube
    assert "tclipsuploadall" in html and "Running" in html
    assert "Old classic" not in html                    # classic Shorts are not shown


def test_shorts_page_filters_by_state(client):
    html = client.get("/shorts").get_data(as_text=True)
    assert "1962 promised Apollo by 1964" in html and "Bacon class" in html and "Doctors Said Never Fly" in html
    sched = client.get("/shorts?state=scheduled").get_data(as_text=True)
    assert "Doctors Said Never Fly" in sched and "1962 promised" not in sched
    page = client.get("/shorts/newsreel-1a").get_data(as_text=True)
    assert "Kennedy at Huntsville" in page and "NARA, no restrictions" in page
    assert client.get("/shorts/nope").status_code == 404


def test_videos_are_served_from_the_maker_folders(client):
    assert client.get("/video/newsreel-1a.mp4").status_code == 200
    r = client.get("/video/nasa-2b.mp4", headers={"Range": "bytes=0-9"})  # the upload plan's copy, seekable
    assert r.status_code == 206 and len(r.data) == 10
    assert client.get("/video/ex-prelinger-2a.mp4").status_code == 404   # no file on disk
    assert client.get("/video/..%2Fmade.json.mp4").status_code == 404


def test_schedule_sources_and_jobs_pages(client):
    assert "WpHmWh-47TY" in client.get("/schedule").get_data(as_text=True)
    src = client.get("/sources").get_data(as_text=True)
    assert "Universal Newsreel 1962" in src and "6 min" in src and "✓" in src
    jobs = client.get("/jobs").get_data(as_text=True)
    assert "the last 10 to historybuffed" in jobs and "old one" in jobs


def test_classic_pages_are_gone(client):
    for path in ("/review", "/make", "/library", "/stats", "/music", "/voices", "/bench"):
        assert client.get(path).status_code == 404, path


def test_pages_are_gzipped_when_the_browser_asks(client):
    import gzip
    plain = client.get("/shorts")
    packed = client.get("/shorts", headers={"Accept-Encoding": "gzip, deflate, br"})
    assert packed.headers["Content-Encoding"] == "gzip" and "Content-Encoding" not in plain.headers
    assert gzip.decompress(packed.data) == plain.data


def test_dashboard_listens_on_localhost_only(settings, monkeypatch):
    from flask import Flask

    from shorts import jobs, web
    seen = {}
    monkeypatch.setattr(Flask, "run", lambda self, **kw: seen.update(kw))
    started = []
    monkeypatch.setattr(jobs.Runner, "start", lambda self: started.append(1))
    web.serve(settings, 5001)
    assert seen["host"] == "127.0.0.1"
    assert not started  # the classic job queue no longer starts


def test_empty_server_still_renders(settings, tmp_path):
    app = create_app(settings, start_runner=False)
    app.config.update(TCLIPS_OUT=str(tmp_path / "none" / "tclips"), TCLIPS_JOBS=str(tmp_path / "none.toml"))
    c = app.test_client()
    for path in ("/", "/shorts", "/schedule", "/sources", "/jobs"):
        assert c.get(path).status_code == 200, path


def test_classicoff_pauses_every_channel(settings, tmp_path, monkeypatch):
    import shutil

    from meta import classicoff
    from shorts.config import CONFIG_DIR, all_channels
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg)
    settings.config_root = cfg
    paused = classicoff.pause_all(settings)
    assert paused and all(ch.paused for ch in all_channels(cfg))
    assert classicoff.pause_all(settings) == []  # again: nothing left to pause
    rep = classicoff.disk_report(settings)
    assert rep["disk"]["total"] > 0 and any(r["label"] == "shorts-data" for r in rep["folders"])
