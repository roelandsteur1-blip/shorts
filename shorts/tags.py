"""Tags in three layers, 3 tags each (Roeland, 2026-10-20):
1. post tags: what this one Short is about ("bicep curl", "bicep workout"); written by the script step;
2. niche tags: the channel's niche, broader ("gym", "fitness", "bodybuilding"); channel settings;
3. broad tags: the same on every Short ("shorts", "viral shorts", "for you"); settings.toml.
All 9 go into the YouTube tags field and, as hashtags, at the end of the description (YouTube
shows the first three of those above the title)."""
from __future__ import annotations

import re

from .config import BROAD_TAGS as BROAD_DEFAULT

PER_LAYER = 3

# Used when a channel has no niche_tags of its own.
NICHE_DEFAULTS = {
    "space": ["space", "astronomy", "nasa"],
    "historybuffed": ["history", "history facts", "old footage"],
    "body": ["human body", "health", "science facts"],
    "earthquaked": ["earthquake", "natural disasters", "geology"],
    "oceanfactoidss": ["ocean", "marine life", "sea creatures"],
    "oldcityfacts": ["old cities", "city history", "vintage footage"],
    "warplanedd": ["warplanes", "military aviation", "fighter jets"],
    "retrotechhh": ["retro tech", "vintage technology", "old gadgets"],
    "weatherwowed": ["weather", "extreme weather", "storms"],
    "planetwildd": ["wildlife", "nature", "animals"],
}


def clean(tag: str) -> str:
    return re.sub(r"\s+", " ", str(tag).strip().lstrip("#").replace(",", " ")).strip()


def pick(tags, n: int = PER_LAYER, skip: set[str] | None = None) -> list[str]:
    """The first n distinct tags, leaving out ones already in skip (case-insensitive)."""
    seen = set(skip or ())
    out = []
    for t in tags or []:
        t = clean(t)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
            if len(out) == n:
                break
    return out


def niche_tags(channel) -> list[str]:
    """All of the channel's niche tags; layers() takes the first 3 that aren't post tags already."""
    own = [t for t in (channel.niche_tags or []) if clean(t)]
    if own:
        return pick(own, 99)
    return list(NICHE_DEFAULTS.get(channel.id, []))


def broad_tags(settings) -> list[str]:
    return pick(getattr(settings, "broad_tags", None) or BROAD_DEFAULT, 99)


def layers(post: list[str], channel, settings) -> tuple[list[str], list[str], list[str]]:
    """(post, niche, broad), 3 each, no tag twice."""
    p = pick(post)
    n = pick(niche_tags(channel), skip={t.lower() for t in p})
    b = pick(broad_tags(settings), skip={t.lower() for t in p + n})
    return p, n, b


def hashtag(tag: str) -> str:
    """'bicep curl' -> '#bicepcurl' (hashtags can't hold spaces or punctuation)."""
    return "#" + re.sub(r"[^\w]", "", clean(tag).lower())
