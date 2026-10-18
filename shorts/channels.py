"""Create and edit channel configs (config/channels/<id>.toml) from the dashboard, and draft one
with Claude from a one-line description."""
from __future__ import annotations

import re
import tomllib
from dataclasses import fields
from pathlib import Path

from . import llm, sources, tomlw
from .config import Channel, Settings, load_channel

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
LANGUAGES = {"en": "English", "nl": "Dutch", "de": "German", "es": "Spanish", "fr": "French"}
HEADER = ("# Channel config. Edited by the dashboard (old version kept as .bak); editing by hand works too.\n"
          "# See config/channels/space.toml for what every field does.")

DEFAULT_STYLE = {
    "font": "Montserrat ExtraBold", "font_size": 88, "primary_color": "&H00FFFFFF",
    "highlight_color": "&H0000D7FF", "outline": 6, "margin_v": 760, "hook_font_size": 104,
}


def path_for(settings: Settings, channel_id: str) -> Path:
    return settings.config_dir / "channels" / f"{channel_id}.toml"


def raw(settings: Settings, channel_id: str) -> dict:
    with open(path_for(settings, channel_id), "rb") as f:
        return tomllib.load(f)


def validate(data: dict, known_categories: set[str]) -> list[str]:
    """Problems that would break the pipeline; empty list = fine."""
    errors = []
    if not ID_RE.match(data.get("id", "")):
        errors.append("id: 2-31 lowercase letters, digits or _, starting with a letter")
    if not (data.get("name") or "").strip():
        errors.append("name is empty")
    if not (data.get("niche") or "").strip():
        errors.append("niche is empty")
    formats = data.get("formats") or []
    if not any(f.get("enabled", True) for f in formats):
        errors.append("at least one format must be switched on")
    ids = [f.get("id") for f in formats]
    if len(set(ids)) != len(ids) or not all(f.get("id") and ID_RE.match(f["id"]) for f in formats):
        errors.append("format ids must be unique, lowercase letters/digits/_")
    if any(not (f.get("title") and f.get("brief")) for f in formats):
        errors.append("every format needs a title and a brief")
    times = data.get("publish_times_utc") or []
    if not times or not all(TIME_RE.match(t) for t in times):
        errors.append("upload times: HH:MM, e.g. 15:00, 21:00")
    try:
        lo, hi = int(data.get("min_seconds", 0)), int(data.get("max_seconds", 0))
        if not 5 <= lo < hi <= 60:
            errors.append("length: 5 <= min < max <= 60 seconds")
        if not 1 <= int(data.get("uploads_per_day", 0)) <= 20:
            errors.append("uploads per day: 1 to 20")
    except (TypeError, ValueError):
        errors.append("length and uploads per day must be numbers")
    try:
        if not 0.7 <= float(data.get("voice_speed", 1.0)) <= 1.3:
            errors.append("voice speed: 0.7 to 1.3")
    except (TypeError, ValueError):
        errors.append("voice speed must be a number")
    unknown = [c for c in data.get("categories") or [] if c not in known_categories]
    if unknown:
        errors.append(f"unknown categories: {', '.join(unknown)}")
    bad_src = [s for s in data.get("sources") or [] if s not in sources.ORDER]
    if bad_src:
        errors.append(f"unknown sources: {', '.join(bad_src)}")
    from .looks import LOOK_NAMES
    look = (data.get("style") or {}).get("look")
    if look and look not in LOOK_NAMES + ["custom"]:
        errors.append(f"unknown caption look: {look}")
    allowed = {f.name for f in fields(Channel)}
    extra = set(data) - allowed
    if extra:
        errors.append(f"unknown fields: {', '.join(sorted(extra))}")
    if not errors:
        try:
            Channel(**data)
        except TypeError as e:
            errors.append(str(e))
    return errors


def save(settings: Settings, data: dict, known_categories: set[str], new: bool = False) -> list[str]:
    """Validate and write; returns the problems instead of writing when there are any."""
    data = {k: v for k, v in data.items() if v not in (None, "")}
    errors = validate(data, known_categories)
    path = path_for(settings, data.get("id", ""))
    if new and path.exists():
        errors.append(f"a channel with id '{data['id']}' already exists")
    if errors:
        return errors
    tomlw.write(path, data, HEADER)
    load_channel(data["id"], settings.config_dir)  # reads back exactly like the pipeline does
    return []


def new_channel(channel_id: str, name: str, language: str, niche: str) -> dict:
    return {
        "id": channel_id, "name": name, "language": language, "niche": niche,
        "categories": [], "sources": ["nasa"], "search_terms": [],
        "min_seconds": 15, "max_seconds": 30, "uploads_per_day": 2,
        "publish_times_utc": ["15:00", "21:00"], "hashtags": ["#shorts"],
        "formats": [{"id": "what_youre_seeing", "title": "What you're actually seeing",
                     "brief": "Explain what is actually happening in the hero shot, the thing most viewers\n"
                              "would not know. Open with a hook that makes the viewer look twice at the image.\n"
                              "Build to one surprising fact near the end."}],
        "style": dict(DEFAULT_STYLE),
    }


DRAFT_PROMPT = """Help set up a new YouTube Shorts channel that makes text-on-screen Shorts (no voice-over)
from public-domain archive footage. Channel name: "{name}". Language: {language}. Niche: "{niche}".

Footage categories available (id: label): {categories}
Footage sources available (id: label): {sources}

Answer with only JSON:
{{"hashtags": ["#..." 2 specific to the niche, then "#shorts" (YouTube shows 3)],
  "categories": ["category ids from the list that fit the niche"],
  "sources": ["source ids from the list that will have this footage; nasa only for space"],
  "search_terms": ["8-15 short search terms for footage archives"],
  "hero_query": "one line describing a scroll-stopping first frame for this niche, for CLIP image search",
  "formats": [{{"id": "lowercase_id", "title": "series name", "brief": "2-3 sentences telling a script writer
     what this series does with the hero shot"}}] (1 to 3 formats)}}
"""


def draft(settings: Settings, name: str, language: str, niche: str, categories: dict[str, str]) -> dict:
    """Claude drafts hashtags, categories, sources, search terms and format briefs. Only known
    category and source ids are kept."""
    src = {m.NAME: m.LABEL for m in sources.all_sources()}
    prompt = DRAFT_PROMPT.format(
        name=name, language=LANGUAGES.get(language, language), niche=niche,
        categories=", ".join(f"{k}: {v}" for k, v in categories.items()),
        sources=", ".join(f"{k}: {v}" for k, v in src.items()))
    data = llm.ask_json(prompt, settings.claude_cmd)
    if not isinstance(data, dict):
        raise llm.LLMError("unexpected reply")
    data["categories"] = [c for c in data.get("categories") or [] if c in categories]
    data["sources"] = [s for s in data.get("sources") or [] if s in src]
    formats = []
    for f in data.get("formats") or []:
        fid = re.sub(r"[^a-z0-9_]+", "_", str(f.get("id") or f.get("title") or "").lower()).strip("_")[:30]
        if fid and f.get("brief"):
            formats.append({"id": fid if fid[0].isalpha() else "f_" + fid, "title": f.get("title") or fid,
                            "brief": f["brief"]})
    data["formats"] = formats[:3]
    return data


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30]
    return s if s[:1].isalpha() else ("ch_" + s)[:30]
