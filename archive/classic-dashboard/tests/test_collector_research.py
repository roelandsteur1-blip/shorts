"""Footage collector research (meta/collector): the test set, every archive adapter against
recorded-shape answers (no internet), failure isolation, sheets, the runner entry and the
Research keys box on the Sources page."""
import tomllib
from collections import Counter

from PIL import Image

from meta.collector import sweep


def test_test_set_has_60_subjects_6_per_niche():
    subs = sweep.load_subjects()
    assert len(subs) == 60
    assert set(Counter(s["niche"] for s in subs).values()) == {6}
    for s in subs:
        assert s["name"] and s["wikidata"] and s["hint"] and s["show"]
    assert len({s["name"] for s in subs}) == 60


def test_wikidata_picks_the_match_with_the_hint_not_the_top_one():
    found = [{"id": "Q1", "label": "Saturn", "description": "Roman god"},
             {"id": "Q193", "label": "Saturn", "description": "sixth planet from the Sun"}]
    assert sweep.pick_qid(found, "planet")[0] == "Q193"
    q, why = sweep.pick_qid(found[:1], "planet")
    assert q is None and "Roman god" in why


def test_commons_licences_and_order():
    ans = {"query": {"searchinfo": {"totalhits": 42}, "pages": {
        "2": {"index": 2, "title": "File:b.webm", "imageinfo": [{"thumburl": "t2", "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}}}]},
        "1": {"index": 1, "title": "File:a.webm", "imageinfo": [{"thumburl": "t1", "duration": 12, "extmetadata": {"LicenseShortName": {"value": "Public domain"}}}]}}}}
    total, hits = sweep.commons("x", "video", g=lambda *a, **k: ans)
    assert total == 42 and [h["title"] for h in hits] == ["File:a.webm", "File:b.webm"]
    assert hits[0]["ok"] and hits[0]["seconds"] == 12 and not hits[1]["ok"]


def test_internet_archive_licence_filter():
    ans = {"response": {"numFound": 3, "docs": [
        {"identifier": "a", "title": "A", "licenseurl": "http://creativecommons.org/publicdomain/mark/1.0/"},
        {"identifier": "b", "title": "B", "licenseurl": "http://creativecommons.org/licenses/by-nc/3.0/"},
        {"identifier": "c", "title": "C"}]}}
    seen = {}
    total, hits = sweep.internet_archive("lion", "mediatype:(movies)", g=lambda url, **p: seen.update(p) or ans)
    assert total == 3 and [h["ok"] for h in hits] == [True, False, False]
    assert "services/img/a" in hits[0]["thumb"] and 'title:("lion")' in seen["q"]


def test_nasa_flags_third_party_footage():
    ans = {"collection": {"metadata": {"total_hits": 2}, "items": [
        {"data": [{"title": "Apollo 11 launch", "nasa_id": "a11"}], "links": [{"render": "image", "href": "th"}]},
        {"data": [{"title": "Launch", "nasa_id": "x", "photographer": "SpaceX"}], "links": []}]}}
    total, hits = sweep.nasa_video("apollo", g=lambda *a, **k: ans)
    assert total == 2 and hits[0]["ok"] and hits[0]["thumb"] == "th" and not hits[1]["ok"]


def test_new_archives_parse():
    svs = {"count": 1, "results": [{"id": 5, "title": "Hurricane", "url": "u", "main_image": {"url": "https://svs.gsfc.nasa.gov/img.jpg"}}]}
    assert sweep.nasa_svs("h", g=lambda *a, **k: svs)[1][0]["thumb"] == "https://svs.gsfc.nasa.gov/img.jpg"
    dv = {"page_info": {"total_results": 7}, "results": [{"title": "F-16", "thumbnail": {"url": "t"}, "url": "u"}]}
    total, hits = sweep.dvids("f-16", "KEY", g=lambda url, **p: dv)
    assert total == 7 and hits[0]["thumb"] == "t" and hits[0]["ok"]
    si = {"response": {"rowCount": 2, "rows": [
        {"title": "Lion", "content": {"descriptiveNonRepeating": {"online_media": {"media": [
            {"type": "Images", "thumbnail": "t", "usage": {"access": "CC0"}}]}}}},
        {"title": "Lion 2", "content": {"descriptiveNonRepeating": {"online_media": {"media": [
            {"type": "Images", "thumbnail": "t2", "usage": {"access": "Usage conditions apply"}}]}}}}]}}
    total, hits = sweep.smithsonian("lion", "K", g=lambda *a, **k: si)
    assert [h["ok"] for h in hits] == [True, False] and hits[0]["kind"] == "photo"
    na = {"body": {"hits": {"total": {"value": 9}, "hits": [{"_source": {"record": {
        "title": "P-51", "naId": 1, "digitalObjects": [{"objectType": "Video (MP4)", "objectUrl": "v.mp4"}]}}}]}}}
    seen = {}
    total, hits = sweep.nara("p-51", "K", g=lambda url, headers=None, **p: seen.update(headers) or na)
    assert total == 9 and hits[0]["ok"] and seen["x-api-key"] == "K"
    ov = {"result_count": 1, "results": [{"title": "Eagle", "license": "cc0", "thumbnail": "t"}]}
    assert sweep.openverse("eagle", g=lambda *a, **k: ov)[1][0]["kind"] == "photo"
    eu = {"totalResults": 1, "items": [{"title": ["Film"], "rights": ["http://creativecommons.org/licenses/by-sa/4.0/"], "edmPreview": ["p"]}]}
    assert not sweep.europeana("x", "K", g=lambda *a, **k: eu)[1][0]["ok"]
    px = {"totalHits": 1, "hits": [{"tags": "heart", "videos": {"medium": {"thumbnail": "t"}}}]}
    assert sweep.pixabay("heart", "K", g=lambda *a, **k: px)[1][0]["thumb"] == "t"


def test_sources_without_a_key_are_skipped_and_one_failure_does_not_stop_the_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "OUT", tmp_path)
    monkeypatch.setattr(sweep, "open_library", lambda: None)
    monkeypatch.setattr(sweep, "wikidata_matches", lambda label: [{"id": "Q1", "label": "x", "description": "y"}])
    monkeypatch.setattr(sweep, "pick_qid", lambda ms, hint: ("Q1", "x: y"))
    monkeypatch.setattr(sweep, "fetch_thumb", lambda src: Image.new("RGB", (64, 36), (90, 90, 90)) if src else None)
    calls = []

    def ok(*a, **k):
        calls.append(a[0])
        return 1, [sweep.hit("t", True, "public-domain", "th")]

    def boom(*a, **k):
        raise RuntimeError("archive down")
    for name in ("commons", "internet_archive", "nasa_svs", "openverse"):
        monkeypatch.setattr(sweep, name, ok)
    monkeypatch.setattr(sweep, "nasa_video", boom)
    sweep.main(["--only", "space", "--limit", "2"])
    import json
    made = json.loads((tmp_path / "made.json").read_text())
    assert len(made["subjects"]) == 2 and set(made["skipped_no_key"]) == {"dvids", "nara", "europeana", "pixabay", "smithsonian"}
    src = made["subjects"][0]["sources"]
    assert "archive down" in src["NASA video"]["error"] and src["NASA SVS"]["licence_ok"] == 1
    assert "DVIDS" not in src
    assert (tmp_path / "sheets" / "space.jpg").exists()
    assert len(list((tmp_path / "sheets" / "subjects").glob("space-*.jpg"))) == 2


def test_runner_allows_the_sweep_job():
    from meta import runner
    cmd = runner.command({"id": "2026-11-04-sweep-1", "name": "sweep"})
    assert cmd[-2:] == ["-m", "meta.collector.sweep"]
    assert runner.command({"id": "2026-11-04-research-tools-1", "name": "research-tools"})[-1] == "meta.collector.tools"


def test_jobs_file_is_valid_toml():
    from pathlib import Path
    jobs = tomllib.loads(Path("meta/jobs.toml").read_text())["job"]
    assert len({j["id"] for j in jobs}) == len(jobs)


def test_research_keys_box_saves_keys(settings):
    from shorts import sources
    from shorts.web import create_app
    client = create_app(settings, start_runner=False).test_client()
    page = client.get("/sources").get_data(as_text=True)
    assert "Research keys" in page and "DVIDS" in page and "No key yet" in page
    r = client.post("/sources-research-key/dvids", data={"api_key": " abc "})
    assert r.status_code == 302 and sources.api_key(settings, "dvids") == "abc"
    assert client.post("/sources-research-key/evil", data={"api_key": "x"}).status_code == 404
    assert "Key saved" in client.get("/sources").get_data(as_text=True)


def test_extra_pass_runs_only_keyed_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "OUT", tmp_path / "sweep")
    monkeypatch.setattr(sweep, "open_library", lambda: None)
    monkeypatch.setattr(sweep, "load_keys", lambda settings: {"smithsonian": "K"})
    monkeypatch.setattr(sweep, "open_library", lambda: (object(), None))
    monkeypatch.setattr(sweep.Scorer, "__init__", lambda self, settings: setattr(self, "enc", None))
    monkeypatch.setattr(sweep, "wikidata_matches", lambda label: [{"id": "Q1", "label": "x", "description": "y"}])
    monkeypatch.setattr(sweep, "pick_qid", lambda ms, hint: ("Q1", "x: y"))
    monkeypatch.setattr(sweep, "fetch_thumb", lambda src: None)
    monkeypatch.setattr(sweep, "smithsonian", lambda q, key: (1, [sweep.hit("t", True, "CC0", None)]))
    monkeypatch.setattr(sweep, "commons", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not keyed")))
    sweep.main(["--extra", "--only", "body", "--limit", "1"])
    import json
    made = json.loads((tmp_path / "sweepx" / "made.json").read_text())
    assert list(made["subjects"][0]["sources"]) == ["Smithsonian"]
    from meta import runner
    assert runner.command({"id": "2026-11-04-sweepx-1", "name": "sweepx"})[-1] == "--extra"


def test_claude_picks_wikidata_items_in_one_call_and_only_from_the_candidates():
    subs = [{"name": "Saturn", "show": "the planet"}, {"name": "lion", "show": "a lion"}, {"name": "x", "show": "y"}]
    ms = [[{"id": "Q1", "label": "Saturn", "description": "Roman god"}, {"id": "Q193", "label": "Saturn", "description": "planet"}],
          [{"id": "Q140", "label": "lion", "description": "species"}], []]
    prompts = []
    got = sweep.claude_qids(subs, ms, lambda p: prompts.append(p) or {"picks": {"1": "Q193", "2": "Q999", "3": "none"}})
    assert got == {0: "Q193", 1: None, 2: None} and len(prompts) == 1 and "Roman god" in prompts[0]


def test_find_image_anywhere_in_a_record():
    assert sweep.find_image({"a": 1, "media": [{"x": "https://svs/a.mp4"}, {"y": "https://svs/b_searchweb.png"}]}) == "https://svs/b_searchweb.png"
    assert sweep.find_image({"a": "not a url.jpg"}) is None
