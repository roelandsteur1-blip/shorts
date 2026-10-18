"""Shared formats (series) a channel draws on next to its own, so no channel posts one template
only (YouTube's inauthentic-content rules, research/shorts-studies.md). Every channel gets a
different mix: the channel's own switched-on formats, topped up to FORMATS_PER_CHANNEL from this
list, starting at a place that depends on the channel id."""
from __future__ import annotations

import zlib

FORMATS_PER_CHANNEL = 3

LIBRARY = [
    {"id": "what_youre_seeing", "title": "What you're actually seeing",
     "brief": "Explain what is actually happening in the hero shot, the thing most viewers would not "
              "know. Open with a hook that makes the viewer look twice at the image. Build to one "
              "surprising fact at the end."},
    {"id": "scale_of", "title": "Scale of...",
     "brief": "Make the viewer feel the size, distance, speed, age or energy of the subject in the hero "
              "shot through one or two concrete comparisons to everyday things. End on the most "
              "striking comparison."},
    {"id": "the_moment", "title": "The moment",
     "brief": "Tell the short story behind the event in the hero shot as a mini drama: the stakes, the "
              "moment itself, then what happened next. The outcome is the payoff at the end."},
    {"id": "myth_vs_fact", "title": "Myth vs fact",
     "brief": "Open with a real, widely held belief about the subject in the hero shot, then show what "
              "is actually true and how we know. Only myths people really believe; never invent one."},
    {"id": "how_it_works", "title": "How it works",
     "brief": "Explain in plain steps how the thing in the hero shot works or was done, building to the "
              "clever or surprising part at the end."},
    {"id": "hidden_detail", "title": "The detail you missed",
     "brief": "Point the viewer to one detail in the hero shot most people never notice, then explain "
              "why it matters. The meaning of the detail is the payoff."},
    {"id": "first_or_record", "title": "First / record",
     "brief": "Show what makes the subject in the hero shot the first, biggest, fastest, oldest or last "
              "of its kind, and why that mattered. Only records you can source."},
]
_BY_ID = {f["id"]: f for f in LIBRARY}


def for_channel(channel) -> list[dict]:
    """The formats the script writer may choose from for this channel."""
    own = channel.enabled_formats
    if not channel.library_formats or len(own) >= FORMATS_PER_CHANNEL:
        return own
    have = {f["id"] for f in own}
    start = zlib.crc32(channel.id.encode()) % len(LIBRARY)
    extra = [f for f in LIBRARY[start:] + LIBRARY[:start] if f["id"] not in have]
    return own + extra[:FORMATS_PER_CHANNEL - len(own)]


def get(format_id: str) -> dict | None:
    return _BY_ID.get(format_id)
