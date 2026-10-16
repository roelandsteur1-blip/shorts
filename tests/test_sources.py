"""Footage sources: search parsing, rights decisions and the shared download step, against canned API
responses in each archive's documented format (the APIs themselves can't be reached from CI)."""
import subprocess

import pytest

from shorts import media, sources
from shorts.sources import commons, common, internet_archive, loc, prelinger

# ---------- a fake requests.Session ----------


class FakeResponse:
    def __init__(self, data=None, content=b"", status=200):
        self.data, self.content, self.status_code = data, content, status
        self.headers = {"Content-Length": str(len(content))} if content else {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    """Answers by the first matching URL fragment; records every request."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        for frag, resp in self.routes.items():
            if frag in url:
                return resp(url, params) if callable(resp) else resp
        return FakeResponse(status=404)


# ---------- canned responses ----------

IA_SEARCH = {"response": {"numFound": 4, "docs": [
    {"identifier": "Apollo11Launch", "title": "Apollo 11 launch", "description": "<p>Saturn V lifts off.</p>",
     "subject": ["space", "rocket"], "licenseurl": "http://creativecommons.org/publicdomain/mark/1.0/",
     "date": "1969-07-16T00:00:00Z", "collection": ["nasa", "movies"]},
    {"identifier": "ccby-clip", "title": "Harbour timelapse", "creator": "Jan Jansen",
     "licenseurl": "https://creativecommons.org/licenses/by/4.0/", "subject": "harbour; ships"},
    {"identifier": "sharealike", "title": "Parade", "licenseurl": "http://creativecommons.org/licenses/by-sa/3.0/"},
    {"identifier": "nolicense", "title": "Some home video"},
]}}

IA_META = {"files": [
    {"name": "Apollo11Launch.ogv", "format": "Ogg Video", "size": "5000000"},
    {"name": "Apollo11Launch_512kb.mp4", "format": "512Kb MPEG4", "size": "3000000", "height": "240"},
    {"name": "Apollo 11 Launch.mp4", "format": "h.264", "size": "9000000", "height": "480"},
    {"name": "Apollo11Launch.mpeg", "format": "MPEG2", "size": "99000000000"},
    {"name": "Apollo11Launch_thumb.jpg", "format": "JPEG"},
]}

LOC_SEARCH = {"results": [
    {"id": "http://www.loc.gov/item/00694297/", "title": "Rocket test, 1946", "subject": ["rockets"],
     "description": ["Test firing of a captured rocket."], "date": "1946", "partof": ["paper print films"]},
    {"id": "https://www.loc.gov/item/2019000001/", "title": "Home movie", "subject": ["families"]},
]}
LOC_ITEMS = {
    "00694297": {"item": {"rights_advisory": "No known restrictions on publication."},
                 "resources": [{"files": [[{"mimetype": "video/mp4", "url": "https://tile.loc.gov/a-small.mp4", "height": 360},
                                           {"mimetype": "video/mp4", "url": "https://tile.loc.gov/a-large.mp4", "height": 720},
                                           {"mimetype": "image/jpeg", "url": "https://tile.loc.gov/a.jpg"}]]}]},
    "2019000001": {"item": {"rights_advisory": "Rights status not evaluated. Permission is required for reuse."},
                   "resources": []},
}

COMMONS = {"query": {"pages": {
    "1": {"pageid": 1, "index": 1, "title": "File:Launch of Falcon.webm", "imageinfo": [{
        "url": "https://upload.wikimedia.org/a/Launch.webm", "descriptionurl": "https://commons.wikimedia.org/wiki/File:Launch.webm",
        "mime": "video/webm", "extmetadata": {"License": {"value": "cc-by-sa-4.0"}, "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                              "Artist": {"value": "<a href='x'>Someone</a>"}}}]},
    "2": {"pageid": 2, "index": 2, "title": "File:Glider flight 1930.ogv", "imageinfo": [{
        "url": "https://upload.wikimedia.org/b/Glider.ogv", "descriptionurl": "https://commons.wikimedia.org/wiki/File:Glider.ogv",
        "mime": "video/ogg", "extmetadata": {"License": {"value": "cc-by-4.0"}, "LicenseShortName": {"value": "CC BY 4.0"},
                                             "Artist": {"value": "<b>Ann Pilot</b>"}, "Categories": {"value": "Gliders|1930s aviation"},
                                             "ImageDescription": {"value": "A glider over hills."}}}]},
    "3": {"pageid": 3, "index": 3, "title": "File:Old film.webm", "imageinfo": [{
        "url": "https://upload.wikimedia.org/c/Old.webm", "mime": "video/webm",
        "extmetadata": {"License": {"value": "pd"}, "LicenseShortName": {"value": "Public domain"}}}]},
    "4": {"pageid": 4, "index": 4, "title": "File:Photo.jpg", "imageinfo": [{"url": "x.jpg", "mime": "image/jpeg"}]},
}}}


# ---------- license helpers ----------

@pytest.mark.parametrize("url,license,reject", [
    ("http://creativecommons.org/publicdomain/mark/1.0/", "public-domain", None),
    ("http://creativecommons.org/licenses/publicdomain/", "public-domain", None),
    ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0", None),
    ("https://creativecommons.org/licenses/by/3.0/", "CC BY 3.0", None),
    ("http://rightsstatements.org/vocab/NoC-US/1.0/", "public-domain", None),
    ("https://creativecommons.org/licenses/by-sa/4.0/", None, "share-alike"),
    ("https://creativecommons.org/licenses/by-nc/4.0/", None, "non-commercial"),
    ("http://rightsstatements.org/vocab/InC/1.0/", None, "in copyright"),
    ("", None, "no license"),
])
def test_license_from_url(url, license, reject):
    r = sources.license_from_url(url)
    assert r.license == license
    assert (reject is None and r.reject is None) or reject in r.reject


@pytest.mark.parametrize("name,license,rejected", [
    ("cc-by-4.0", "CC BY 4.0", False), ("CC BY 2.0", "CC BY 2.0", False), ("pd", "public-domain", False),
    ("PD-USGov", "public-domain", False), ("cc-zero", "CC0", False), ("Public domain", "public-domain", False),
    ("cc-by-sa-4.0", None, True), ("CC BY-SA 3.0", None, True), ("cc-by-nc-sa-2.0", None, True),
    ("GFDL", None, True),
])
def test_license_from_name(name, license, rejected):
    r = sources.license_from_name(name)
    assert r.license == license and bool(r.reject) == rejected


# ---------- per source ----------

def test_internet_archive_search_and_rights():
    s = FakeSession({"advancedsearch": FakeResponse(IA_SEARCH), "/metadata/": FakeResponse(IA_META)})
    items = internet_archive.search("rocket", session=s)
    q = dict(s.calls[0][1])["q"]
    assert q.startswith("(rocket) AND mediatype:(movies)") and "-collection:(prelinger)" in q
    by_id = {i.source_id: i for i in items}
    assert by_id["Apollo11Launch"].description == "Saturn V lifts off."
    assert by_id["Apollo11Launch"].source_url == "https://archive.org/details/Apollo11Launch"
    assert "collection:nasa" in by_id["Apollo11Launch"].keywords
    assert by_id["ccby-clip"].keywords[:2] == ["harbour", "ships"]
    verdicts = {i.source_id: internet_archive.rights(i) for i in items}
    assert verdicts["Apollo11Launch"].license == "public-domain" and not verdicts["Apollo11Launch"].attribution
    assert verdicts["ccby-clip"].attribution == "Jan Jansen / Internet Archive (CC BY 4.0)"
    assert "share-alike" in verdicts["sharealike"].reject
    assert verdicts["nolicense"].reject == "no license given"
    # mp4 first, the largest one that fits; never the oversized original.
    assert internet_archive.file_url(by_id["Apollo11Launch"], s) == \
        "https://archive.org/download/Apollo11Launch/Apollo%2011%20Launch.mp4"


def test_prelinger_is_its_own_collection():
    s = FakeSession({"advancedsearch": FakeResponse(IA_SEARCH)})
    items = prelinger.search("subject:(war)", session=s)
    assert "collection:(prelinger)" in dict(s.calls[0][1])["q"] and "-collection" not in dict(s.calls[0][1])["q"]
    assert {i.source for i in items} == {"prelinger"}


def test_loc_only_free_to_use():
    s = FakeSession({"film-and-videos": FakeResponse(LOC_SEARCH),
                     "/item/": lambda url, params: FakeResponse(LOC_ITEMS[url.rstrip("/").rsplit("/", 1)[1]])})
    items = loc.search("rocket", session=s)
    assert [i.source_id for i in items] == ["00694297", "2019000001"]
    ok = common.check(items[0], s, None)
    assert ok[1].license == "public-domain" and ok[0].file_url == "https://tile.loc.gov/a-large.mp4"
    bad = common.check(items[1], s, None)
    assert bad[1].reject


def test_commons_rights_per_file():
    s = FakeSession({"api.php": FakeResponse(COMMONS)})
    items = commons.search("launch", session=s)
    assert len(items) == 3  # the jpg is not a video
    r = [commons.rights(i) for i in items]
    assert "share-alike" in r[0].reject
    assert r[1].license == "CC BY 4.0" and r[1].attribution == "Ann Pilot / Wikimedia Commons (CC BY 4.0)"
    assert items[1].keywords == ["Gliders", "1930s aviation"] and items[1].description == "A glider over hills."
    assert r[2].license == "public-domain" and items[2].file_url.endswith("Old.webm")


# ---------- shared ingest ----------

@pytest.fixture
def clip_bytes(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25",
                    "-f", "lavfi", "-i", "sine=f=440", "-t", "6", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True)
    return path.read_bytes()


def test_ingest_downloads_usable_and_records_rejections(conn, settings, clip_bytes, monkeypatch):
    s = FakeSession({"advancedsearch": FakeResponse(IA_SEARCH), "/metadata/": FakeResponse(IA_META),
                     "/download/": FakeResponse(content=clip_bytes)})
    monkeypatch.setattr(common.requests, "Session", lambda: s)
    n = common.ingest(conn, settings, "internet_archive", ["rocket"], per_term=5, max_pages=1)
    # PD and CC BY are usable; BY-SA and unlicensed are stored as rejected, never downloaded.
    # The fake archive serves the same clip twice, so the second one is caught as a duplicate.
    assert n == 1
    rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM assets")}
    assert rows["ccby-clip"]["status"] == "duplicate"
    assert rows["sharealike"]["status"] == "rejected" and "share-alike" in rows["sharealike"]["error"]
    assert rows["nolicense"]["status"] == "rejected"
    assert rows["ccby-clip"]["attribution"] == "Jan Jansen / Internet Archive (CC BY 4.0)"
    done = rows["Apollo11Launch"]
    assert done["status"] == "downloaded" and done["license"] == "public-domain"
    assert not media.probe(done["path"])["has_audio"]
    # A second run finds nothing new and doesn't re-judge the rejected ones.
    assert common.ingest(conn, settings, "internet_archive", ["rocket"], per_term=5, max_pages=1) == 0


def test_source_settings(settings):
    assert sources.usable(settings, "nasa") == (True, "")
    sources.set_enabled(settings, "loc", False)
    assert sources.usable(settings, "loc")[0] is False
    sources.set_enabled(settings, "loc", True)
    assert sources.usable(settings, "loc")[0] is True
    sources.set_api_key(settings, "pexels", "abc ")
    assert sources.api_key(settings, "pexels") == "abc"
    assert (settings.secrets_dir / "api_keys.json").exists()
