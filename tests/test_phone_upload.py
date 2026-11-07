"""Upload via phone (shorts/drive.py): no music, approved Shorts to Google Drive, the upload from
the phone is found by title and its Drive files go to the trash."""
import dataclasses
import json
from datetime import datetime, timezone

from shorts import channels as chans, db, drive, music, upload
from shorts.config import load_channel
from shorts.web import create_app

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


class FakeReq:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeDrive:
    """Just enough of the Drive v3 client: files().list/create/update."""

    def __init__(self):
        self.files_ = {}  # id -> {name, parents, mimeType, trashed, text}
        self.n = 0

    def files(self):
        return self

    def list(self, q, **kw):
        hits = [{"id": i} for i, f in self.files_.items()
                if f"name = '{f['name']}'" in q and f.get("mimeType") == drive.FOLDER and not f["trashed"]
                and (f"'{f['parents'][0]}' in parents" in q)]
        return FakeReq({"files": hits[:1]})

    def create(self, body, media_body=None, fields=None):
        self.n += 1
        fid = f"f{self.n}"
        text = None
        if media_body is not None and media_body.mimetype() == "text/plain":
            text = media_body.getbytes(0, media_body.size()).decode("utf-8")
        self.files_[fid] = dict(body, trashed=False, text=text)
        return FakeReq({"id": fid})

    def update(self, fileId, body):
        self.files_[fileId].update(body)
        return FakeReq({})


def phone(channel):
    return dataclasses.replace(channel, upload_via="phone", publish_times_utc=["17:00"], uploads_per_day=1)


def _approved(conn, tmp_path, title="Why octopuses have three hearts"):
    video = tmp_path / f"{abs(hash(title))}.mp4"
    video.write_bytes(b"mp4")
    sid = conn.execute("""INSERT INTO shorts (channel, format_id, title, description, tags, status, video_path)
                          VALUES ('space', 'what_youre_seeing', ?, 'A fact.', '["octopus"]', 'approved', ?)""",
                       (title, str(video))).lastrowid
    conn.commit()
    return sid


def test_phone_channel_gets_no_music(conn, settings, channel):
    assert music.pick(conn, settings, 1, phone(channel)) is None
    from meta.textcard import pick_track
    assert pick_track(conn, {"no_music": True}) is None


def test_channel_setting_saved(settings, channel):
    assert channel.upload_via == "api"
    data = chans.raw(settings, "space")
    data["upload_via"] = "phone"
    assert chans.validate(data, set(data.get("categories") or [])) == []


def test_approved_shorts_go_to_drive(conn, settings, channel, tmp_path, monkeypatch):
    monkeypatch.setattr("shorts.render.metadata", lambda c, ch, sid, s: {
        "title": "Why octopuses have three hearts", "description": "A fact.", "tags": ["octopus", "ocean"]})
    ch = phone(channel)
    sid = _approved(conn, tmp_path)
    drv = FakeDrive()
    assert drive.send_approved(conn, settings, ch, drv=drv, now=NOW) == [sid]
    row = conn.execute("SELECT * FROM shorts WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "on_drive" and row["publish_at"] == "2026-11-07T17:00:00Z"
    ids = json.loads(row["drive_files"])
    names = {drv.files_[i]["name"] for i in ids}
    assert names == {f"11-07 1800 Why octopuses have three hearts ({sid}).mp4",
                     f"11-07 1800 Why octopuses have three hearts ({sid}).txt"}
    folders = [f for f in drv.files_.values() if f.get("mimeType") == drive.FOLDER]
    assert [f["name"] for f in folders] == ["Shorts for phone", ch.name]
    note = next(drv.files_[i]["text"] for i in ids if drv.files_[i]["name"].endswith(".txt"))
    assert "Why octopuses have three hearts" in note and "18:00" in note and "octopus, ocean" in note
    # the slot is taken: the next Short gets the next day
    assert upload.next_slots(conn, ch, 1, now=NOW)[0].day == 8
    # a second run makes no new folders
    sid2 = _approved(conn, tmp_path, "Mars has the tallest volcano")
    monkeypatch.setattr("shorts.render.metadata", lambda c, ch, s, st: {"title": "Mars has the tallest volcano",
                                                                        "description": "", "tags": []})
    drive.send_approved(conn, settings, ch, drv=drv, now=NOW)
    assert len([f for f in drv.files_.values() if f.get("mimeType") == drive.FOLDER]) == 2
    assert conn.execute("SELECT status FROM shorts WHERE id=?", (sid2,)).fetchone()[0] == "on_drive"


def test_upload_from_phone_found_and_drive_cleaned(conn, settings, channel, tmp_path):
    ch = phone(channel)
    sid = _approved(conn, tmp_path)
    drv = FakeDrive()
    f1 = drv.create({"name": "a.mp4", "parents": ["x"]}).execute()["id"]
    db.set_short(conn, sid, status="on_drive", drive_files=json.dumps([f1]), publish_at="2026-11-07T17:00:00Z")
    items = {
        "other1": {"snippet": {"title": "Something else"}, "status": {"privacyStatus": "public"}},
        "vid123": {"snippet": {"title": "Why octopuses have THREE hearts! #shorts #ocean"},
                   "status": {"privacyStatus": "private", "publishAt": "2026-11-07T17:00:00Z"}},
    }
    assert drive.match(conn, ch, items, NOW) == {"vid123": sid}
    row = conn.execute("SELECT * FROM shorts WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "scheduled" and row["youtube_id"] == "vid123"
    assert drive.match(conn, ch, items, NOW) == {}  # matched once only
    assert drive.cleanup(conn, settings, drv=drv) == 1
    assert drv.files_[f1]["trashed"] is True
    assert conn.execute("SELECT drive_files FROM shorts WHERE id=?", (sid,)).fetchone()[0] is None


def test_upload_all_sends_phone_channels_to_drive(conn, settings, channel, tmp_path, monkeypatch):
    sid = _approved(conn, tmp_path)
    sent = []
    monkeypatch.setattr(drive, "send_approved", lambda c, s, ch, limit=None: sent.append((ch.id, limit)) or [sid])
    monkeypatch.setattr("shorts.config.load_channel", lambda cid, d=None: phone(load_channel(cid, d)))
    monkeypatch.setattr("shorts.config.list_channels", lambda d=None: ["space"])
    monkeypatch.setattr(upload, "fetch_stats_all", lambda c, s: {})
    out = upload.upload_all(conn, settings)
    assert sent == [("space", None)] and "to Google Drive" in out["space"]


def test_channel_page_shows_phone_option(settings):
    app = create_app(settings, start_runner=False)
    html = app.test_client().get("/channels/space").get_data(as_text=True)
    assert 'name="upload_via"' in html and "Via my phone" in html


def test_norm_ignores_case_hashtags_and_punctuation():
    assert drive.norm("Why *octopuses* have 3 hearts?! 🐙 #shorts") == drive.norm("why octopuses have 3 hearts")


def test_upload_without_drive_login_skips_cleanly(conn, settings, channel, tmp_path):
    sid = _approved(conn, tmp_path)
    assert upload.upload_approved(conn, settings, phone(channel)) == []  # no token-drive.json
    assert conn.execute("SELECT status FROM shorts WHERE id=?", (sid,)).fetchone()[0] == "approved"
