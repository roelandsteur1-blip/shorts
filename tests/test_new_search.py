"""The new footage search (embed.py 'pe' space): Perception Encoder vectors on 3 frames per scene,
scored on the best frame, switched on once nearly the whole library has them."""
import json
import sqlite3

import numpy as np

from conftest import FakeEncoder
from shorts import embed


def _unit(seed, dim=FakeEncoder.dim):
    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _scene(conn, n, frames, title=""):
    aid = conn.execute("INSERT INTO assets (source, source_id, license, title) VALUES ('t', ?, 'pd', ?)",
                       (str(n), title)).lastrowid
    return conn.execute("INSERT INTO scenes (asset_id, start, end, embedding, pe) VALUES (?, 0, 5, ?, ?)",
                        (aid, _unit(100 + n).tobytes(), embed._pe_blob(frames))).lastrowid


def test_library_gets_vectors_and_search_switches_over(library, settings):
    st = embed.pe_status(library)
    assert st["scenes"] and st["ready"] == st["scenes"] and st["unreadable"] == 0
    blob = library.execute("SELECT pe FROM scenes WHERE rejected = 0 LIMIT 1").fetchone()[0]
    assert len(blob) == len(embed.PE_SPOTS) * FakeEncoder.dim * 2
    assert embed.search_space(library, settings) == "pe"
    assert embed.search(library, settings, "saturn rings", k=3)
    settings.search_pe = False
    assert embed.search_space(library, settings) == "clip"
    settings.search_pe = True
    # Halfway through the background job: still CLIP, then the job finishes and PE takes over.
    ids = [r[0] for r in library.execute("SELECT id FROM scenes WHERE rejected = 0 ORDER BY id")]
    gone = ids[:max(1, len(ids) // 4)]
    library.execute(f"UPDATE scenes SET pe = NULL WHERE id IN ({','.join('?' * len(gone))})", gone)
    library.commit()
    assert embed.search_space(library, settings) == "clip"
    assert embed.embed_pe(library, settings) == len(gone)
    assert embed.search_space(library, settings) == "pe"


def test_unreadable_video_is_marked_and_left_out(library, settings):
    sid = library.execute("SELECT id FROM scenes WHERE rejected = 0 ORDER BY id LIMIT 1").fetchone()[0]
    library.execute("UPDATE assets SET path = '/nowhere.mp4' WHERE id = (SELECT asset_id FROM scenes WHERE id=?)",
                    (sid,))
    library.execute("UPDATE scenes SET pe = NULL WHERE id=?", (sid,))
    library.commit()
    embed.embed_pe(library, settings)
    assert library.execute("SELECT pe FROM scenes WHERE id=?", (sid,)).fetchone()[0] == b""
    assert embed.pe_status(library)["unreadable"] >= 1
    assert sid not in [s for s, _ in embed.search(library, settings, "saturn", k=100, space="pe")]


def test_pe_search_counts_the_best_frame(conn, settings):
    q = FakeEncoder().texts(["red fox"])[0]
    flat = _scene(conn, 1, [_unit(1)] * 3)
    peak = _scene(conn, 2, [_unit(2), q, _unit(3)])  # the fox is in one of its three frames
    for n in range(3, 12):
        _scene(conn, n, [_unit(10 * n + k) for k in range(3)])
    conn.commit()
    hits = embed.search(conn, settings, "red fox", k=3, space="pe")
    assert hits[0][0] == peak and abs(hits[0][1] - 1.0) < 0.01
    assert flat in [s for s, _ in embed.search(conn, settings, "red fox", k=20, space="pe")]
    # The title bonus is in standard deviations of the query's scores, as the bench measured it.
    titled = _scene(conn, 20, [_unit(7)] * 3, title="red fox in the snow")
    conn.commit()
    plain = float(np.max(np.stack([_unit(7)] * 3) @ q))
    got = dict(embed.search(conn, settings, "red fox", k=50, space="pe"))[titled]
    assert got > plain + 0.01
    v = embed.scene_vector(conn, "pe", peak)
    assert abs(np.linalg.norm(v) - 1) < 1e-3 and v.shape == q.shape


def test_bench_vectors_give_the_job_a_head_start(conn, settings):
    a = _scene(conn, 1, [])
    b = _scene(conn, 2, [])
    conn.execute("UPDATE scenes SET pe = NULL")
    conn.commit()
    folder = settings.data_dir / "bench"
    (folder / "emb").mkdir(parents=True)
    snap = sqlite3.connect(str(folder / "shorts.db"))
    conn.backup(snap)
    snap.close()
    conn.execute("UPDATE scenes SET start = 1 WHERE id=?", (b,))  # changed since: not the same scene
    conn.commit()
    img = np.stack([np.stack([_unit(1), _unit(2), _unit(3)])] * 2).astype(np.float16)
    valid = np.array([[True, True, False], [True, True, True]])
    np.savez(folder / "emb" / "pe-b16.npz", ids=np.array([a, b]), img=img, valid=valid)
    assert embed.import_bench_pe(conn, settings) == 1
    got = np.frombuffer(conn.execute("SELECT pe FROM scenes WHERE id=?", (a,)).fetchone()[0], np.float16)
    assert got.reshape(3, -1)[2].tolist() == img[0, 1].tolist()  # the missing frame repeats one read
    assert conn.execute("SELECT pe FROM scenes WHERE id=?", (b,)).fetchone()[0] is None


def test_calibrate_reads_the_threshold_off_the_judge(settings):
    from shorts import bench
    from shorts.bench import calibrate, models
    root = bench.root(settings)
    (root / "emb").mkdir()
    q = _unit(0)
    ids = list(range(10, 30))
    labels = {}
    for name in ("clip-b32", "pe-b16"):
        img = np.stack([np.stack([_unit(i + k) for k in range(3)]) for i in ids])
        on = ids[:5]
        for j, sid in enumerate(ids):
            if sid in on:
                img[j, 1] = (q + 0.2 * _unit(sid)) / np.linalg.norm(q + 0.2 * _unit(sid))
        np.savez(models.emb_path(settings, name), ids=np.array(ids), img=img.astype(np.float16),
                 valid=np.ones((len(ids), 3), bool), queries=q[None], block_ids=np.array([1]))
        labels = {"1": {str(s): [2 if s in on else 0, 3] for s in ids}}
    (root / "judge.json").write_text(json.dumps(labels))
    ts = {"blocks": [{"id": 1, "query": "red fox"}], "scenes": {str(s): {"title": "", "keywords": ""} for s in ids}}
    out = calibrate.run(settings, ts)
    assert out["clip"]["pairs"] == out["pe"]["pairs"] == len(ids)
    assert out["pe"]["same_on_topic_kept"]["on_topic_kept"] >= out["clip"]["today"]["on_topic_kept"]
    assert out["bonus_scale"] > 0
