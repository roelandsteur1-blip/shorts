"""Footage sources. One module per source with the same small interface:

    NAME, LABEL, LICENSE           id, display name, license summary for the Sources page
    NEEDS_KEY                      True: the API needs a (free) key, entered on the Sources page
    HERO_OK                        False for stock B-roll: allowed for text blocks, never as hero shot
    FIXED_CATEGORIES               categories every video of this source belongs to (NASA: space)
    search(query, page, session, key) -> list[Item]
    enrich(item, session, key) -> Item      optional: fetch details needed by rights()/file_url()
    rights(item) -> Rights                  license and credit, or why we can't use it
    file_url(item, session, key) -> str     the video file to download

`common.ingest` does the rest (store, rights check, download, strip audio, dedupe) for all of them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from importlib import import_module
from types import ModuleType

from ..config import Settings

# Display order on the Sources page. Import-folder sources have no API (sources/folder.py).
ORDER = ["nasa", "internet_archive", "prelinger", "loc", "commons", "europeana", "openbeelden",
         "pexels", "pixabay", "coverr", "mixkit", "dareful", "vidsplay"]
FOLDER_SOURCES = {"mixkit", "dareful", "vidsplay"}
OFF_BY_DEFAULT = {"coverr"}  # its terms ban AI use of the videos; skip until CLIP search is cleared


@dataclass
class Item:
    source: str
    source_id: str
    title: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)  # archive subjects/tags (also used for categories)
    creator: str = ""
    source_url: str = ""           # human page for the item
    date_created: str | None = None
    license_hint: str = ""         # raw license field (URL or name) as the archive gives it
    rights_text: str = ""          # free-text rights statement, if any
    file_url: str | None = None    # when the search result already names the file
    extra: dict = field(default_factory=dict)


@dataclass
class Rights:
    license: str | None = None     # 'public-domain', 'CC0', 'CC BY 4.0', 'Pexels License', ...
    attribution: str | None = None # credit that must appear in the description, if any
    reject: str | None = None      # reason we can't use it (CC BY-SA, no license, ...)
    needs_check: bool = False      # usable license, but something needs a human look first


def get(name: str) -> ModuleType:
    if name in FOLDER_SOURCES:
        from . import folder
        return folder.source(name)
    if name not in ORDER:
        raise KeyError(f"unknown source {name!r}; known: {', '.join(ORDER)}")
    return import_module(f".{name}", __name__)


def all_sources() -> list[ModuleType]:
    return [get(n) for n in ORDER]


def stock_sources() -> list[str]:
    return [m.NAME for m in all_sources() if not getattr(m, "HERO_OK", True)]


def fixed_categories(name: str) -> list[str]:
    try:
        return list(getattr(get(name), "FIXED_CATEGORIES", []))
    except KeyError:
        return []


def label(name: str) -> str:
    try:
        return get(name).LABEL
    except KeyError:
        return name


# ---------- per-laptop source settings: on/off and API keys ----------

def _read(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def disabled(settings: Settings) -> set[str]:
    data = _read(settings.data_dir / "sources.json")
    return set(data.get("disabled", sorted(OFF_BY_DEFAULT)))


def set_enabled(settings: Settings, name: str, on: bool) -> None:
    off = disabled(settings)
    (off.discard if on else off.add)(name)
    path = settings.data_dir / "sources.json"
    data = _read(path) | {"disabled": sorted(off)}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def api_key(settings: Settings, name: str) -> str | None:
    return _read(settings.secrets_dir / "api_keys.json").get(name) or None


def set_api_key(settings: Settings, name: str, key: str) -> None:
    path = settings.secrets_dir / "api_keys.json"
    data = _read(path)
    if key:
        data[name] = key.strip()
    else:
        data.pop(name, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def usable(settings: Settings, name: str) -> tuple[bool, str]:
    """Can restock download from this source right now? (ok, reason if not)"""
    try:
        mod = get(name)
    except KeyError:
        return False, "unknown source"
    if name in disabled(settings):
        return False, "switched off on the Sources page"
    if getattr(mod, "NEEDS_KEY", False) and not api_key(settings, name):
        return False, "needs an API key (Sources page)"
    if name in FOLDER_SOURCES:
        return False, "no API: use the import folder"
    return True, ""


# ---------- license helpers shared by the source modules ----------

def strip_html(text) -> str:
    if isinstance(text, list):
        text = " ".join(str(t) for t in text)
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", unescape(text)).strip()


def as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for v in value:
            out += as_list(v)
        return out
    return [p.strip() for p in re.split(r"[;|]", str(value)) if p.strip()]


SHARE_ALIKE = "CC BY-SA (share-alike: the Short would have to be CC BY-SA too; skipped)"


def license_from_url(url: str | None) -> Rights:
    """Creative Commons / RightsStatements URL -> Rights. Anything unrecognised is rejected."""
    u = (url or "").lower().strip()
    if not u:
        return Rights(reject="no license given")
    if "publicdomain/mark" in u or "licenses/publicdomain" in u or "/noc-us" in u or "/noc-oklr" in u:
        return Rights(license="public-domain")
    if "publicdomain/zero" in u:
        return Rights(license="CC0")
    m = re.search(r"creativecommons\.org/licenses/([a-z-]+)/(\d\.\d)", u)
    if m:
        kind, version = m.groups()
        if kind == "by":
            return Rights(license=f"CC BY {version}")
        if kind == "by-sa":
            return Rights(reject=SHARE_ALIKE)
        return Rights(reject=f"CC {kind.upper()} (non-commercial or no-derivatives)")
    if "rightsstatements.org" in u:
        return Rights(reject="in copyright or rights unclear (rightsstatements.org)")
    return Rights(reject=f"license not recognised: {url}")


def license_from_name(name: str | None) -> Rights:
    """Short license names as Wikimedia Commons and others give them: 'cc-by-4.0', 'CC BY-SA 3.0',
    'pd', 'Public domain', 'cc0', 'cc-zero'."""
    n = (name or "").lower().strip().replace("_", "-")
    if not n:
        return Rights(reject="no license given")
    if n in ("cc0", "cc-zero", "cc0 1.0", "cc0-1.0") or n.startswith("cc0"):
        return Rights(license="CC0")
    if n.startswith("pd") or "public domain" in n:
        return Rights(license="public-domain")
    m = re.match(r"cc[ -]by([ -]sa|[ -]nc[a-z-]*|[ -]nd)?[ -]?(\d\.\d)?", n)
    if m:
        variant, version = m.groups()
        if not variant:
            return Rights(license=f"CC BY {version or ''}".strip())
        if "sa" in variant and "nc" not in variant:
            return Rights(reject=SHARE_ALIKE)
        return Rights(reject="CC BY-NC/ND (non-commercial or no-derivatives)")
    return Rights(reject=f"license not recognised: {name}")


def cc_by_attribution(rights: Rights, creator: str, where: str) -> Rights:
    """CC BY needs a credit line in the description: '<creator> / <where> (CC BY 4.0)'."""
    if rights.license and rights.license.startswith("CC BY"):
        who = " / ".join(p for p in (creator.strip(), where) if p)
        rights.attribution = f"{who} ({rights.license})"
    return rights
