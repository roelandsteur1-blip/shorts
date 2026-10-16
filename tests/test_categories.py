"""Sorting footage into categories per scene, and channels only using their own categories."""
import json
import shutil

import numpy as np
import pytest

from conftest import FakeEncoder, fake_claude
from shorts import categorize, hero, llm, pipeline, render
from shorts.config import ROOT, load_channel

ENC = FakeEncoder()


def vec(text):
    return ENC.texts([text])[0]


def add_video(conn, source, title, scene_vecs, keywords="", description="", keyframe="kf.jpg"):
    aid = conn.execute("""INSERT INTO assets (source, source_id, title, keywords, description, license, status)
                          VALUES (?,?,?,?,?, 'public-domain', 'scened')""",
                       (source, title, title, keywords, description)).lastrowid
    ids = []
    for i, v in enumerate(scene_vecs):
        ids.append(conn.execute("""INSERT INTO scenes (asset_id, start, end, keyframe, motion, contrast, embedding)
                                   VALUES (?,?,?,?,0.03,0.2,?)""",
                                (aid, i * 6.0, i * 6.0 + 6, keyframe, np.asarray(v, np.float32).tobytes())).lastrowid)
    conn.commit()
    return aid, ids


def cats_of(conn, scene_id):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT category, method FROM scene_categories WHERE scene_id=? AND confidence > 0", (scene_id,))}


def test_metadata_scores(settings):
    cats = categorize.load_categories(settings)
    m = categorize.metadata_scores({"title": "WWII bomber crews over Germany", "keywords": "aviation, aircraft",
                                    "description": "Soldiers load the planes."}, cats)
    assert m["war"] >= categorize.META_SURE and m["aviation"] >= categorize.META_SURE
    assert "ocean" not in m
    # Whole words only: 'warm' is not 'war'.
    assert "war" not in categorize.metadata_scores({"title": "A warm day", "keywords": "", "description": ""}, cats)


def test_sorting_steps(conn, settings, monkeypatch):
    space = vec("a rocket launching into the sky")
    parade = vec("a crowd at a historic parade")
    noise = np.random.default_rng(5).standard_normal(32)
    noise /= np.linalg.norm(noise)

    _, nasa = add_video(conn, "nasa", "Artemis launch", [noise])
    # Metadata says war + aviation; CLIP sees a war scene: they agree on war.
    _, bomber = add_video(conn, "internet_archive", "WWII bomber film", [vec("soldiers on a battlefield")],
                          keywords="war, aviation, military")
    # A newsreel about a rocket test (metadata: space) with a parade scene in it: CLIP is sure the
    # parade is history, metadata is sure it's space -> they disagree -> Claude decides per video.
    _, reel = add_video(conn, "prelinger", "Rocket test day, 1952", [space, parade],
                        keywords="space, rocket, launch")
    # No metadata, CLIP sure: CLIP decides.
    _, ocean = add_video(conn, "commons", "clip0042", [vec("underwater with fish and coral")])
    # Nothing to go on (no metadata, CLIP unsure) and Claude has no answer: uncategorised.
    vague = np.random.default_rng(30).standard_normal(32)
    vague /= np.linalg.norm(vague)
    assert categorize.clip_probs(conn, settings, np.stack([vague]).astype(np.float32),
                                 categorize.load_categories(settings)).max() < categorize.CLIP_SURE
    _, unknown = add_video(conn, "commons", "clip0043", [vague])

    asked = []

    def ask(prompt, claude_cmd="claude", timeout=600):
        asked.append(prompt)
        lines = [l for l in prompt.splitlines() if l[:1].isdigit()]
        out = []
        for l in lines:
            n = int(l.split(".")[0])
            out.append({"n": n, "categories": ["space", "history"] if "Rocket test day" in l else []})
        return json.dumps(out)
    monkeypatch.setattr(llm, "ask", ask)

    counts = categorize.sort_pending(conn, settings)
    assert cats_of(conn, nasa[0]) == {"space": "source"}
    assert "war" in cats_of(conn, bomber[0])
    assert cats_of(conn, reel[0]) == {"space": "metadata+clip"}  # rocket scene: both agree
    assert cats_of(conn, reel[1]) == {"history": "claude+clip"}
    assert cats_of(conn, ocean[0]) == {"ocean": "clip"}
    assert cats_of(conn, unknown[0]) == {}
    assert len(asked) == 1  # one batched call for everything unsure
    assert counts["uncategorised"] >= 1
    # Nothing left to sort: no second Claude call.
    categorize.sort_pending(conn, settings)
    assert len(asked) == 1

    # A correction by hand moves the scene and survives a full re-sort.
    categorize.move_scene(conn, reel[1], "everyday")
    assert cats_of(conn, reel[1]) == {"everyday": "manual"}
    categorize.resort(conn)
    categorize.sort_pending(conn, settings)
    assert cats_of(conn, reel[1]) == {"everyday": "manual"}
    rollup = dict(conn.execute("SELECT category, scenes FROM asset_categories WHERE asset_id=(SELECT asset_id FROM scenes WHERE id=?)",
                               (reel[1],)).fetchall())
    assert rollup == {"space": 1, "everyday": 1}


def test_claude_unavailable_leaves_scenes_for_later(conn, settings, monkeypatch):
    vague = np.random.default_rng(30).standard_normal(32)  # CLIP unsure, see test_sorting_steps
    _, ids = add_video(conn, "commons", "clip", [vague / np.linalg.norm(vague)])

    def fail(*a, **k):
        raise llm.LLMError("limit reached")
    monkeypatch.setattr(llm, "ask", fail)
    categorize.sort_pending(conn, settings)
    assert conn.execute("SELECT sorted FROM scenes WHERE id=?", (ids[0],)).fetchone()[0] == 0


def test_hand_sorted_scenes_teach_clip(conn, settings):
    look = np.random.default_rng(9).standard_normal(32)
    look /= np.linalg.norm(look)
    _, a = add_video(conn, "commons", "example", [look])
    categorize.set_manual(conn, a[0], "transport")
    near = look + 0.05 * np.random.default_rng(10).standard_normal(32)
    p = categorize.clip_probs(conn, settings, np.stack([near / np.linalg.norm(near)]).astype(np.float32),
                              categorize.load_categories(settings))
    ids = [c.id for c in categorize.load_categories(settings)]
    assert p[0, ids.index("transport")] >= 0.5


def test_channel_only_uses_its_categories(library, settings, channel, monkeypatch):
    """A space channel never opens with or cuts to a war scene, even a striking one."""
    war_scene = library.execute("SELECT id FROM scenes WHERE rejected=0 ORDER BY id DESC LIMIT 1").fetchone()[0]
    library.execute("DELETE FROM scene_categories WHERE scene_id=?", (war_scene,))
    library.execute("INSERT INTO scene_categories VALUES (?, 'war', 1, 'manual')", (war_scene,))
    library.commit()
    assert war_scene not in hero.candidates(library, settings, channel.id, 50, categories=["space"])
    assert hero.candidates(library, settings, channel.id, 50, categories=["war"]) == [war_scene]
    fake_claude(monkeypatch)
    sid = pipeline.make(library, settings, channel, count=1)[0]
    used = {r[0] for r in library.execute("SELECT scene_id FROM blocks WHERE short_id=?", (sid,))}
    assert war_scene not in used


def test_credit_lines_for_other_archives(conn, channel):
    aid = conn.execute("""INSERT INTO assets (source, source_id, title, license, attribution, status)
                          VALUES ('commons', '2', 'Glider flight 1930', 'CC BY 4.0',
                                  'Ann Pilot / Wikimedia Commons (CC BY 4.0)', 'scened')""").lastrowid
    aid2 = conn.execute("""INSERT INTO assets (source, source_id, title, license, status)
                           VALUES ('prelinger', 'x', 'Duck and cover', 'public-domain', 'scened')""").lastrowid
    s1 = conn.execute("INSERT INTO scenes (asset_id, start, end) VALUES (?,0,5)", (aid,)).lastrowid
    s2 = conn.execute("INSERT INTO scenes (asset_id, start, end) VALUES (?,0,5)", (aid2,)).lastrowid
    sid = conn.execute("""INSERT INTO shorts (channel, format_id, hero_scene_id, title, description, tags)
                          VALUES ('space', 'f', ?, 'T', 'D', '[]')""", (s1,)).lastrowid
    conn.executemany("INSERT INTO blocks (short_id, idx, text, seconds, scene_id) VALUES (?,?,?,?,?)",
                     [(sid, 0, "a", 2, s1), (sid, 1, "b", 2, s2)])
    conn.commit()
    desc = render.metadata(conn, channel, sid)["description"]
    assert "- Ann Pilot / Wikimedia Commons (CC BY 4.0): Glider flight 1930" in desc
    assert "- Prelinger Archives (public-domain): Duck and cover" in desc
    assert "NASA does not endorse" not in desc and "http" not in desc


# ---------- pages ----------

@pytest.fixture
def cfg(tmp_path, settings):
    """A copy of config/ the pages may write to."""
    d = tmp_path / "config"
    shutil.copytree(ROOT / "config", d, ignore=shutil.ignore_patterns("local.toml"))
    settings.config_root = d
    return d


@pytest.fixture
def client(settings, cfg):
    from shorts.web import create_app
    return create_app(settings, start_runner=False).test_client()


def test_library_and_sources_pages(client, conn, settings, cfg, monkeypatch, tmp_path):
    kf = tmp_path / "kf.jpg"
    kf.write_bytes(b"\xff\xd8jpeg")
    _, ids = add_video(conn, "commons", "clip", [vec("x")], keyframe=str(kf))
    conn.execute("UPDATE scenes SET sorted=1 WHERE id=?", (ids[0],))
    conn.commit()
    page = client.get("/library").data
    assert b"War &amp; military" in page and b"Uncategorised" in page
    assert b"/keyframe/" in client.get("/library/uncategorised").data
    assert client.get(f"/keyframe/{ids[0]}").status_code == 200
    client.post(f"/scene/{ids[0]}/category", data={"category": "aviation", "action": "move"})
    assert cats_of(conn, ids[0]) == {"aviation": "manual"}
    assert b"clip" in client.get("/library/aviation").data
    assert client.post(f"/scene/{ids[0]}/category", data={"category": "nope"}).status_code == 400

    monkeypatch.setattr(llm, "ask", lambda *a, **k: json.dumps(
        {"id": "trains", "label": "Trains", "keywords": ["train", "railway"], "clip_prompts": ["a steam train"],
         "search": {"internet_archive": "subject:(railroads)"}}))
    assert client.post("/library/draft", data={"name": "Trains"}).get_json()["id"] == "trains"
    client.post("/library/categories", data={"id": "trains", "label": "Trains", "keywords": "train, Railway",
                                             "clip_prompts": "a steam train\na station",
                                             "search": "internet_archive = subject:(railroads)\nloc = railroads"})
    cats = {c.id: c for c in categorize.load_categories(settings)}
    assert cats["trains"].keywords == ["train", "railway"]
    assert cats["trains"].search == {"internet_archive": "subject:(railroads)", "loc": "railroads"}
    assert (cfg / "categories.toml.bak").exists() and "war" in cats

    page = client.get("/sources").data
    assert b"Library of Congress" in page and b"Switch off" in page
    client.post("/sources/loc", data={"enabled": "0"})
    assert b"switched off on the Sources page" in client.get("/sources").data
    r = client.post("/sources/commons/try", data={"query": "glider"})
    job = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert json.loads(job["command"]) == ["sources", "--try", "commons", "glider"] and job["heavy"] == 0
    r = client.post("/library/sort", data={"all": "1"})
    assert json.loads(conn.execute("SELECT command FROM jobs ORDER BY id DESC").fetchone()[0]) == ["categorize", "--all"]


def test_space_channel_config():
    ch = load_channel("space")
    assert ch.categories == ["space"] and ch.sources == ["nasa"]


def test_stock_footage_is_never_a_hero(conn, settings):
    _, ids = add_video(conn, "pexels", "waves", [vec("ocean waves crashing")])
    conn.execute("UPDATE scenes SET end = start + 6")
    conn.commit()
    assert ids[0] not in hero.candidates(conn, settings, "space", 10, use_clip=False)
    _, own = add_video(conn, "prelinger", "waves film", [vec("ocean waves crashing")])
    assert own[0] in hero.candidates(conn, settings, "space", 10, use_clip=False)
