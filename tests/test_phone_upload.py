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


def _block(conn, sid, text):
    conn.execute("INSERT INTO blocks (short_id, idx, text, seconds) VALUES (?, 0, ?, 3)", (sid, text))
    conn.commit()


def test_approved_shorts_go_to_drive(conn, settings, channel, tmp_path, monkeypatch):
    monkeypatch.setattr("shorts.render.metadata", lambda c, ch, sid, s: {
        "title": "Why octopuses have three hearts", "description": "A fact.", "tags": ["octopus", "ocean"]})
    ch = phone(channel)
    sid = _approved(conn, tmp_path)
    _block(conn, sid, "*Octopuses* have three hearts, and two stop when they swim.")
    drv = FakeDrive()
    assert drive.send_approved(conn, settings, ch, drv=drv, now=NOW) == [sid]
    row = conn.execute("SELECT * FROM shorts WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "on_drive" and row["publish_at"] == "2026-11-07T17:00:00Z"
    assert row["phone_key"] == "Octopuses have three"
    ids = json.loads(row["drive_files"])
    assert [drv.files_[i]["name"] for i in ids] == ["Octopuses have three - 07-11 18.00.mp4"]  # no .txt
    folders = [f for f in drv.files_.values() if f.get("mimeType") == drive.FOLDER]
    assert [f["name"] for f in folders] == ["Shorts for phone"]
    # the slot is taken: the next Short gets the next day
    assert upload.next_slots(conn, ch, 1, now=NOW)[0].day == 8
    # a Short starting with the same 3 words waits until the first is posted; another one goes, no new folders
    sid2 = _approved(conn, tmp_path, "Octopus blood is blue")
    _block(conn, sid2, "Octopuses have three kinds of blood? No: one, and it is blue.")
    sid3 = _approved(conn, tmp_path, "Mars volcano")
    _block(conn, sid3, "Mars has the tallest volcano.")
    assert drive.send_approved(conn, settings, ch, drv=drv, now=NOW) == [sid3]
    assert len([f for f in drv.files_.values() if f.get("mimeType") == drive.FOLDER]) == 1
    assert conn.execute("SELECT status FROM shorts WHERE id=?", (sid2,)).fetchone()[0] == "approved"
    assert conn.execute("SELECT phone_key FROM shorts WHERE id=?", (sid3,)).fetchone()[0] == "Mars has the"


class FakeYT:
    def __init__(self):
        self.updates = []

    def videos(self):
        return self

    def update(self, part, body):
        self.updates.append((part, body))
        return FakeReq({})


def test_typed_words_matched_and_title_set(conn, settings, channel, tmp_path, monkeypatch):
    monkeypatch.setattr("shorts.render.metadata", lambda c, ch, sid, s: {
        "title": "Why octopuses have three hearts", "description": "A fact.", "tags": ["octopus"]})
    ch = phone(channel)
    a, b = _approved(conn, tmp_path), _approved(conn, tmp_path, "Blue blood")
    db.set_short(conn, a, status="on_drive", phone_key="Octopuses have three", publish_at="2026-11-07T17:00:00Z",
                 duration=21.4, drive_at="2026-11-07T10:00:00Z")
    db.set_short(conn, b, status="on_drive", phone_key="Blue blood flows", publish_at="2026-11-08T17:00:00Z",
                 duration=27.0, drive_at="2026-11-07T10:00:00Z")
    items = {"v1": {"snippet": {"title": "blue blood flows #shorts"}, "status": {"privacyStatus": "private"}},
             # typo in the words: found by its length (21 s)
             "v2": {"snippet": {"title": "Octopus have tree", "publishedAt": "2026-11-07T11:00:00Z"},
                    "status": {"privacyStatus": "private"}, "contentDetails": {"duration": "PT21S"}},
             # same length but posted before it went to Drive: not it
             "old": {"snippet": {"title": "?", "publishedAt": "2026-11-01T11:00:00Z"},
                     "status": {"privacyStatus": "public"}, "contentDetails": {"duration": "PT27S"}}}
    assert drive.match(conn, ch, items, NOW) == {"v1": b, "v2": a}
    yt = FakeYT()
    assert drive.fill(conn, settings, ch, yt, {"v1": b}, items, NOW) == 0  # no manage login yet: nothing changed
    upload.token_path(settings, ch.id).write_text(json.dumps({"scopes": upload.scopes_for(ch)}), encoding="utf-8")
    assert drive.fill(conn, settings, ch, yt, drive.unfilled(conn, ch), items, NOW) == 2
    part, body = next(u for u in yt.updates if u[1]["id"] == "v1")
    assert part == "snippet,status" and body["snippet"]["title"] == "Why octopuses have three hearts"
    assert body["status"]["publishAt"] == "2026-11-08T17:00:00Z" and body["status"]["privacyStatus"] == "private"
    row = conn.execute("SELECT status, filled FROM shorts WHERE id=?", (b,)).fetchone()
    assert (row["status"], row["filled"]) == ("scheduled", 1)
    assert drive.unfilled(conn, ch) == {}


def test_fill_keeps_his_own_schedule(conn, settings, channel, tmp_path, monkeypatch):
    monkeypatch.setattr("shorts.render.metadata", lambda c, ch, sid, s: {"title": "T", "description": "", "tags": []})
    ch = phone(channel)
    upload.token_path(settings, ch.id).write_text(json.dumps({"scopes": upload.scopes_for(ch)}), encoding="utf-8")
    sid = _approved(conn, tmp_path)
    items = {"v": {"snippet": {"title": "x"}, "status": {"privacyStatus": "private", "publishAt": "2026-11-09T10:00:00Z"}}}
    yt = FakeYT()
    assert drive.fill(conn, settings, ch, yt, {"v": sid}, items, NOW) == 1
    assert yt.updates[0][0] == "snippet" and "status" not in yt.updates[0][1]


def test_api_channels_keep_their_scopes(channel):
    assert upload.MANAGE not in upload.scopes_for(channel)
    assert upload.MANAGE in upload.scopes_for(phone(channel))


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


def test_one_channel_at_a_time_on_the_phone(conn, settings, channel, tmp_path, monkeypatch):
    monkeypatch.setattr("shorts.render.metadata", lambda c, ch, sid, s: {"title": "T", "description": "", "tags": []})
    space = phone(channel)
    ocean = dataclasses.replace(space, id="ocean", name="Ocean")
    a = _approved(conn, tmp_path, "Space one")
    b = _approved(conn, tmp_path, "Ocean one")
    conn.execute("UPDATE shorts SET channel='ocean' WHERE id=?", (b,))
    conn.commit()
    drv = FakeDrive()
    assert drive.send_approved(conn, settings, space, drv=drv, now=NOW) == [a]
    assert drive.send_approved(conn, settings, ocean, drv=drv, now=NOW) == []  # space is still on the phone
    assert drive.on_phone(conn)["channel"] == "space"
    monkeypatch.setattr("shorts.config.all_channels", lambda d=None: [space, ocean])
    assert drive.send_next(conn, settings, drv=drv, now=NOW) == []  # not empty yet
    db.set_short(conn, a, status="scheduled", youtube_id="v1")  # posted
    assert drive.send_next(conn, settings, drv=drv, now=NOW) == [b]
    assert drive.on_phone(conn)["channel"] == "ocean"
