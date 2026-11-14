"""The transcript-first dashboard (shorts/web/tfirst.py) on files like the maker and runner leave."""
import pathlib
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


def test_classicpurge_deletes_only_the_fixed_list(settings, tmp_path, monkeypatch):
    from meta import classicpurge
    home = tmp_path / "home"
    monkeypatch.setattr(classicpurge, "HOME", home)
    data = settings.data_dir
    for d in ("footage/nasa", "renders", "keyframes", "bench", "music", "backups", "secrets"):
        (data / d).mkdir(parents=True, exist_ok=True)
        (data / d / "f").write_bytes(b"x" * 10)
    (data / "shorts.db").write_bytes(b"db")
    for d in ("tclips", "transcripts", "videos", "clips", "pieces", "quiz"):
        (home / "meta-out" / d).mkdir(parents=True)
        (home / "meta-out" / d / "f").write_bytes(b"x")
    (home / "meta-out" / "samples.tar.gz").write_bytes(b"x")
    (home / "bench-venv").mkdir()
    (home / "research-venv").mkdir()
    assert classicpurge.purge(settings, dry=True) and (data / "footage").exists()  # dry run deletes nothing
    gone = {pathlib.Path(r["path"]).name for r in classicpurge.purge(settings)}
    assert gone == {"footage", "renders", "keyframes", "bench", "bench-venv", "pieces", "quiz", "samples.tar.gz"}
    for kept in (data / "music", data / "backups", data / "secrets", data / "shorts.db", home / "research-venv",
                 *(home / "meta-out" / d for d in ("tclips", "transcripts", "videos", "clips"))):
        assert kept.exists(), kept
    assert not classicpurge.safe(data, settings) and not classicpurge.safe(home, settings)


def test_channels_page_make_button_runs_one_make_at_a_time(client, out, monkeypatch):
    import sys
    import time

    from shorts.web import tfirst
    base = out[0]
    html = client.get("/channels").get_data(as_text=True)
    for name in ("Weird Newsreels", "Old School Advice", "Yesterday&#39;s Future", "Space Race Files", "Classic Movie Moments"):
        assert name in html
    # the make itself: a short script that writes one finished Short like the maker does
    script = ("import json, pathlib, sys; d = pathlib.Path(sys.argv[1]) / 'channels' / 'weirdnewsreels'; "
              "d.mkdir(parents=True, exist_ok=True); (d / 'weirdnewsreels-11141200-1-newsreel-1a.mp4').write_bytes(b'v'); "
              "(d / 'made.json').write_text(json.dumps([{'short': 'weirdnewsreels-11141200-1-newsreel-1a', "
              "'channel': 'weirdnewsreels', 'kind': 'newsreel', 'hook': 'Robot butler of 1957'}])); "
              "print('[channel] READY 1 of 1')")
    monkeypatch.setattr(tfirst, "command_for", lambda meta: [sys.executable, "-c", script, str(base)])
    r = client.post("/channels/weirdnewsreels/make", data={"n": "3"})
    assert r.status_code == 302 and "Started" in r.headers["Location"]
    r = client.post("/channels/classicmoments/make", data={"n": "99"})  # second one waits its turn
    assert "Queued" in r.headers["Location"]
    jobs = tfirst.dash_jobs(base)
    assert [j["channel"] for j in jobs if j["state"] == "queued"] == ["classicmoments"] and jobs[-1]["channel"] == "weirdnewsreels"
    assert next(j for j in jobs if j["channel"] == "classicmoments")["n"] == 10  # capped
    assert jobs[-1]["n"] == 3
    for _ in range(100):
        if any(j["state"] == "done" for j in tfirst.dash_jobs(base)):
            break
        time.sleep(0.1)
    done = next(j for j in tfirst.dash_jobs(base) if j["channel"] == "weirdnewsreels")
    assert done["state"] == "done" and done["ready"] == 1
    html = client.get("/channels").get_data(as_text=True)
    assert "Robot butler of 1957" in html
    assert client.get("/video/weirdnewsreels-11141200-1-newsreel-1a.mp4").status_code == 200
    assert "Robot butler" in client.get("/shorts?channel=weirdnewsreels").get_data(as_text=True)
    queued = next(j for j in tfirst.dash_jobs(base) if j["channel"] == "classicmoments")
    client.post(f"/channels/jobs/{queued['id']}/stop")                 # cancel the queued one
    assert all(j["channel"] != "classicmoments" for j in tfirst.dash_jobs(base))
    assert client.post("/channels/nope/make").status_code == 404


def test_tclips_channel_step_makes_n_shorts_from_the_channel_kinds(tmp_path, monkeypatch):
    from meta import tclips
    chans = {c["id"]: c for c in tclips.load_tchannels()}
    assert set(chans) == {"weirdnewsreels", "oldschooladvice", "yesterdaysfuture", "spaceracefiles", "classicmoments"}
    assert chans["yesterdaysfuture"]["kinds"] == ["newsreel", "prelinger", "nasa"]
    monkeypatch.setattr(tclips, "OUT", tmp_path)
    monkeypatch.setattr(tclips, "CH_DIR", tmp_path / "channels")
    monkeypatch.setattr(tclips, "PLAN", tmp_path / "upload-plan.json")
    import meta.common
    monkeypatch.setattr(meta.common, "open_library", lambda: (None, None, None))
    monkeypatch.setattr(tclips, "load_titles", lambda: ([], []))
    monkeypatch.setattr(tclips.tx, "get", lambda s: {})
    asked = []
    monkeypatch.setattr(tclips, "channel_sources", lambda kind, n, t, d: [{"id": f"{kind}-src{i}", "title": "T"} for i in range(n)])

    def fake_make_source(src, kind, si, settings, llm, per_source, bare, ex):
        asked.append((kind, ex["label"].split(":")[0], "PREDICTS" in ex["moments"]))
        return [{"short": f"{ex['prefix']}{kind}-{si + 1}a", "source": src["id"], "sentences": [1, 2],
                 "lengths": {f"{ex['prefix']}{kind}-{si + 1}a.mp4": 20.0}}]
    monkeypatch.setattr(tclips, "make_source", fake_make_source)
    made = tclips.channel_make("yesterdaysfuture", 4)
    assert len(made) == 4 and [a[0] for a in asked] == ["newsreel", "newsreel", "prelinger", "nasa"]
    assert all(a[1] == "Yesterday's Future" and a[2] for a in asked)
    saved = json.loads((tmp_path / "channels" / "yesterdaysfuture" / "made.json").read_text())
    assert len(saved) == 4 and all(m["channel"] == "yesterdaysfuture" and m["short"].startswith("yesterdaysfuture-") for m in saved)


def test_shorts_page_groups_and_sorts_by_channel(client, out):
    base = out[0]
    d = base / "channels" / "spaceracefiles"
    d.mkdir(parents=True)
    (d / "made.json").write_text(json.dumps([{"short": "spaceracefiles-11141300-1-nasa-1a", "channel": "spaceracefiles",
                                              "kind": "nasa", "hook": "Astronaut joke"}]))
    html = client.get("/shorts").get_data(as_text=True)
    # grouped in the order of config/tchannels.toml, then historybuffed, then the old tests
    assert html.index("<h2>Space Race Files") < html.index("<h2>historybuffed") < html.index("<h2>Tests and examples")
    assert "Doctors Said Never Fly" in html[html.index("<h2>historybuffed"):html.index("<h2>Tests and examples")]
    only = client.get("/shorts?channel=spaceracefiles").get_data(as_text=True)
    assert "Astronaut joke" in only and "Doctors Said Never Fly" not in only
    flat = client.get("/shorts?sort=publish").get_data(as_text=True)
    assert "<h2>" not in flat.split("<main>")[1].split("</main>")[0].replace("<h1>", "")


def test_archive_moves_test_shorts_but_keeps_the_upload_plan(tmp_path, monkeypatch):
    from meta import tclips
    monkeypatch.setattr(tclips, "OUT", tmp_path)
    (tmp_path / "made.json").write_text(json.dumps([{"short": "nasa-1a", "source": "s1", "sentences": [3, 5]}]))
    (tmp_path / "nasa-1a.mp4").write_bytes(b"v")
    (tmp_path / "examples").mkdir()
    (tmp_path / "upload").mkdir()
    (tmp_path / "upload-plan.json").write_text("[]")
    (tmp_path / "channels").mkdir()
    monkeypatch.setattr(tclips, "PLAN", tmp_path / "upload-plan.json")
    dest = tclips.archive_current()
    assert not (tmp_path / "made.json").exists() and not (tmp_path / "nasa-1a.mp4").exists()
    assert (dest / "nasa-1a.mp4").exists() and (dest / "examples").is_dir()
    assert (tmp_path / "upload").is_dir() and (tmp_path / "upload-plan.json").exists() and (tmp_path / "channels").is_dir()
    assert tclips.used_lines() == {"s1": [(3, 5)]}  # moved Shorts still count as used lines
    assert tclips.archive_current() is None
