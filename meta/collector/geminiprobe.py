"""Why does Gemini 3.8 Flash time out in the judge test? (6 Nov 2026) Read-only runner job
"geminiprobe": one round-2 subject, the same judge request sent in a few variants, each timed,
with Google's token counts (prompt, thoughts, answer) and finish reason. Output in the job log and
~/meta-out/geminiprobe/made.json.

Variants: today's request (temperature 0, default thinking), no temperature, no temperature +
thinking low, and one clip instead of ten. Both 3.8 Flash and 3 Flash, for comparison."""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from pathlib import Path

import requests

OUT = Path.home() / "meta-out" / "geminiprobe"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def main() -> None:
    from meta.collector import judgetest
    from meta.common import open_library
    from shorts import judge
    from shorts.sources import api_key
    settings, _, _ = open_library()
    key = api_key(settings, "gemini")
    if not key:
        raise SystemExit("no Gemini key")
    OUT.mkdir(parents=True, exist_ok=True)
    s = judgetest.load_subjects()[0]
    clips = [{"label": c["label"], "path": c["path"], "start": 0.0, "seconds": 8.0} for c in s["clips"]]
    videos = {c["label"]: base64.b64encode(judge.excerpt(c["path"], 0.0, 8.0)).decode("ascii") for c in clips}
    log(f"subject {s['key']}, {len(clips)} clips, {sum(len(v) for v in videos.values()) / 1e6:.1f} MB base64")
    subj, era = judge._rules(s["niche"], s["name"])

    def parts(cl):
        p = [{"text": judge.GEMINI_PROMPT.format(niche=s["niche"], name=s["name"], show="",
                                                 labels=", ".join(c["label"] for c in cl), subject_rule=subj, era_rule=era)}]
        for c in cl:
            p += [{"text": f"Clip {c['label']}:"}, {"inline_data": {"mime_type": "video/mp4", "data": videos[c["label"]]}}]
        return p

    variants = [
        ("today: temperature 0, default thinking", {"responseMimeType": "application/json", "temperature": 0}, clips),
        ("no temperature, default thinking", {"responseMimeType": "application/json"}, clips),
        ("no temperature, thinking low", judge.gemini_config("low"), clips),
        ("no temperature, thinking low, 1 clip", judge.gemini_config("low"), clips[:1]),
    ]
    rows = []
    for model in ("gemini-3.8-flash", "gemini-3-flash"):
        try:
            mid = judge.gemini_model(key, model)
        except Exception as e:  # noqa: BLE001
            log(f"{model}: {e}")
            continue
        info = requests.get(f"{judge.API}/models/{mid}", params={"key": key}, timeout=30).json()
        log(f"{model} -> {mid}: input limit {info.get('inputTokenLimit')}, thinking {info.get('thinking')}")
        for name, cfg, cl in variants:
            t0 = time.time()
            row = {"model": mid, "variant": name, "clips": len(cl)}
            try:
                r = requests.post(f"{judge.API}/models/{mid}:generateContent", headers={"x-goog-api-key": key},
                                  json={"contents": [{"role": "user", "parts": parts(cl)}], "generationConfig": cfg},
                                  timeout=600)
                row["http"] = r.status_code
                d = r.json()
                u = d.get("usageMetadata", {})
                row.update(prompt_tokens=u.get("promptTokenCount"), thought_tokens=u.get("thoughtsTokenCount"),
                           answer_tokens=u.get("candidatesTokenCount"),
                           finish=(d.get("candidates") or [{}])[0].get("finishReason"),
                           error=(d.get("error") or {}).get("message", "")[:200])
                try:
                    text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
                    row["parsed"] = len(judge._parse(text, [c["label"] for c in cl]))
                except (KeyError, IndexError, ValueError):
                    row["parsed"] = 0
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
            row["seconds"] = round(time.time() - t0, 1)
            rows.append(row)
            log(json.dumps(row))
            (OUT / "made.json").write_text(json.dumps({"subject": s["key"], "rows": rows}, indent=1), encoding="utf-8")
            time.sleep(10)


if __name__ == "__main__":
    main()
