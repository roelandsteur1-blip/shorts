"""report.md: one table per job, best first, with what each method costs on this server."""
from __future__ import annotations

import math

import numpy as np

from . import load, root, save, snapshot
from .metrics import auc, spearman, topic_scores, window_scores
from .models import MODELS, load_emb


def _f(v, pct: bool = True) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{100 * v:.0f}%" if pct else f"{v:.2f}"


def build(settings) -> str:
    r = root(settings)
    ts = load(r / "testset.json", {})
    ranks = load(r / "rank.json", {})
    labels = load(r / "judge.json", {})
    clips = load(r / "clips.json", {})
    interest = load(r / "interest.json", {})
    conn = snapshot(settings)
    lib = conn.execute("SELECT COUNT(*) FROM scenes WHERE rejected = 0 AND keyframe IS NOT NULL").fetchone()[0]
    lines = ["# Clip quality bench: results", "",
             f"Test set: {len(ts.get('blocks', []))} blocks, {len(ts.get('scenes', {}))} library scenes. "
             f"Judge labels: {len(labels)} blocks. Clips for seconds/crop: "
             f"{sum(1 for d in clips.values() if 'judge' in d)} judged. Real library: {lib} scenes.", ""]

    # Cost per model
    cost = {}
    lines += ["## Models: CPU cost on this server", "",
              "| Model | s per image | one-time library, 1 frame | 3 frames | load s | RAM peak |",
              "|---|---|---|---|---|---|"]
    for name, (_, _, label) in MODELS.items():
        e = load_emb(settings, name)
        if e is None:
            lines.append(f"| {label} | not run | | | | |")
            continue
        spi = float(e["secs_per_image"])
        cost[name] = spi
        lines.append(f"| {label} | {spi:.3f} | {lib * spi / 3600:.1f} h | {3 * lib * spi / 3600:.1f} h | "
                     f"{float(e['load_seconds']):.0f} | {float(e['peak_rss_mb']):.0f} MB |")
    lines.append("")

    # On topic
    rows = []
    for method, per_block in ranks.items():
        s = topic_scores(per_block, labels)
        if s["n"]:
            rows.append((method, s))
    rows.sort(key=lambda x: (-(x[1]["p1"] if not math.isnan(x[1]["p1"]) else -1), -x[1]["hit5"]))
    lines += ["## 1. On topic (finding the clip)", "",
              "p1 = first clip on topic; p1 related = at least related; hit5 = an on-topic clip in the "
              "first 5 (what a menu for Claude needs); covered = share of the first 5 the judge saw.", "",
              "| Method | blocks | p1 | p1 related | hit5 | nDCG@5 | covered |", "|---|---|---|---|---|---|---|"]
    for m, s in rows:
        lines.append(f"| {m} | {s['n']} | {_f(s['p1'])} | {_f(s['p1_any'])} | {_f(s['hit5'])} | "
                     f"{_f(s['ndcg5'], False)} | {_f(s['covered'])} |")
    lines.append("")

    # Can a minimum score separate on-topic from off-topic? (AUC of the raw middle-frame score)
    lines += ["### Can a minimum match score work?", "",
              "AUC = chance an on-topic clip scores above an off-topic one for the same kind of query "
              "(0.5 = coin flip, 1 = perfect). Only a high AUC makes a fixed threshold (like MIN_MATCH) safe.", "",
              "| Model | AUC (topic 2 vs 0) |", "|---|---|"]
    blocks = {str(b["id"]): i for i, b in enumerate(ts.get("blocks", []))}
    for name, (_, _, label) in MODELS.items():
        e = load_emb(settings, name)
        if e is None:
            continue
        index = {str(i): j for j, i in enumerate(e["ids"])}
        pos, neg = [], []
        for key, lab in labels.items():
            if key not in blocks:
                continue
            q = e["queries"][blocks[key]].astype(np.float32)
            for sid, (topic, _) in lab.items():
                if sid in index:
                    v = float(e["img"][index[sid], 1].astype(np.float32) @ q)
                    (pos if topic == 2 else neg if topic == 0 else []).append(v)
        lines.append(f"| {label} | {_f(auc(pos, neg), False)} |")
    lines.append("")

    # Seconds
    wres: dict[str, list[dict]] = {}
    for d in clips.values():
        j = d.get("judge")
        if not j:
            continue
        for m, w in d["w"].items():
            if "start" in w:
                s = window_scores(j["frame_times"], w["start"], d["seconds"], j["frames"], j.get("best_start"))
                if s:
                    wres.setdefault(m, []).append(s | {"secs": w.get("secs", 0.0)})
    lines += ["## 2. The right seconds", "",
              "in view = how much of the shown part has the subject in view (judge, per half second); "
              "all clear = the subject is clearly in view the whole time; off by = seconds from the "
              "judge's best start.", "",
              "| Method | clips | in view | all clear | off by (s) | CPU s per clip |", "|---|---|---|---|---|---|"]
    for m, v in sorted(wres.items(), key=lambda x: -np.mean([s["in_view"] for s in x[1]])):
        off = [s["off_by"] for s in v if "off_by" in s]
        lines.append(f"| {m} | {len(v)} | {_f(np.mean([s['in_view'] for s in v]))} | "
                     f"{_f(np.mean([s['all_clear'] for s in v]))} | {np.mean(off) if off else float('nan'):.1f} | "
                     f"{np.mean([s['secs'] for s in v]):.2f} |")
    lines.append("")

    # Crops
    cres: dict[str, list] = {}
    for d in clips.values():
        j = d.get("judge")
        if not j:
            continue
        for letter, names in j["letters"].items():
            rating = j["crops"].get(letter)
            if rating is None:
                continue
            for m in names:
                c = d["s"][m]
                cres.setdefault(m, []).append((rating, letter == j.get("best_crop"), c.get("secs", 0.0),
                                               c.get("found"), c.get("whole")))
    lines += ["## 3. The right crop", "",
              "rating: 2 = subject fully in the crop and well placed, 1 = partly, 0 = missing. best = the "
              "judge would show this one. found = the detector found something (else it fell back to saliency).", "",
              "| Method | clips | mean rating | rated 2 | rated 0 | best | whole frame | found | CPU s per clip |",
              "|---|---|---|---|---|---|---|---|---|"]
    for m, v in sorted(cres.items(), key=lambda x: -np.mean([t[0] for t in x[1]])):
        ratings = np.array([t[0] for t in v])
        found = [t[3] for t in v if t[3] is not None]
        lines.append(f"| {m} | {len(v)} | {ratings.mean():.2f} | {_f((ratings == 2).mean())} | "
                     f"{_f((ratings == 0).mean())} | {_f(np.mean([t[1] for t in v]))} | "
                     f"{_f(np.mean([bool(t[4]) for t in v]))} | {_f(np.mean(found)) if found else '-'} | "
                     f"{np.mean([t[2] for t in v]):.2f} |")
    lines.append("")

    # Interest
    judged = interest.get("judged", {})
    lines += ["## 4. Visually interesting", "",
              "Spearman = how well the score ranks clips like the judge's interest rating (1-5); "
              "top 20% = the judge's average interest for the 20% of clips the score likes most "
              f"(all clips: {np.mean(list(judged.values())) if judged else float('nan'):.2f}).", "",
              "| Score | clips | Spearman | top 20% |", "|---|---|---|---|"]
    srows = []
    for name, vals in interest.get("scores", {}).items():
        common = [s for s in vals if s in judged]
        if len(common) < 10:
            continue
        a = np.array([vals[s] for s in common])
        b = np.array([judged[s] for s in common])
        top = b[np.argsort(-a)[:max(1, len(a) // 5)]].mean()
        sign = -1 if name == "busy" else 1  # busy frames are expected to bore
        srows.append((name, len(common), spearman(sign * a, b), top))
    for name, n, rho, top in sorted(srows, key=lambda x: -(x[2] if not math.isnan(x[2]) else -9)):
        lines.append(f"| {name}{' (less is better)' if name == 'busy' else ''} | {n} | {rho:.2f} | {top:.2f} |")
    lines.append("")
    human = load(r / "human.json", {})
    if human:
        pairs = [(labels.get(k.split(":")[0], {}).get(k.split(":")[1]), v) for k, v in human.items()]
        pairs = [(j, h) for j, h in pairs if j]
        same = np.mean([j[0] == h[0] for j, h in pairs]) if pairs else float("nan")
        same2 = np.mean([(j[0] == 2) == (h[0] == 2) for j, h in pairs]) if pairs else float("nan")
        yes = [j[1] for j, h in pairs if h[1] == 1]
        no = [j[1] for j, h in pairs if h[1] == 0]
        lines += ["## Does the judge agree with Roeland?", "",
                  f"{len(pairs)} clips checked. Same topic label: {_f(same)}. Same answer to "
                  f"\"clearly on topic or not\": {_f(same2)}. Judge's interest for clips Roeland would keep "
                  f"watching: {np.mean(yes) if yes else float('nan'):.1f}, for the rest: "
                  f"{np.mean(no) if no else float('nan'):.1f}.", ""]
    asr_meta = load(r / "asr-meta.json", {})
    if asr_meta:
        ok = [m for m in asr_meta.values() if "asr_s" in m]
        audio = sum(m["audio_s"] for m in ok)
        lines += ["## Narration (Whisper)", "",
                  f"{len(ok)} of {len(asr_meta)} videos had audio; {sum(1 for m in ok if m['speech'])} had speech. "
                  f"Transcribing took {sum(m['asr_s'] for m in ok) / 60:.0f} min for {audio / 60:.0f} min of audio "
                  f"({audio / max(sum(m['asr_s'] for m in ok), 1):.0f}x faster than real time); fetching the audio "
                  f"{sum(m['fetch_s'] for m in asr_meta.values() if 'fetch_s' in m) / 60:.0f} min.", ""]
    text = "\n".join(lines)
    (r / "report.md").write_text(text, encoding="utf-8")
    save(r / "summary.json", {"topic": {m: s for m, s in rows}})
    return text
