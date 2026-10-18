"""Caption looks: every channel gets its own font and outline-or-box style, so the channels don't
look like one template (YouTube's inauthentic-content rules, research/shorts-studies.md). The
highlight colour is the same yellow on every channel (Roeland, 18 Oct 2026)."""
from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
HIGHLIGHT = "&H0000D7FF"  # yellow (ASS colours are BGR)

# Caption box: x 60-880 keeps text clear of YouTube's like/comment/share buttons on the right
# (safe area x 48-888, y 288-1248 on 1080x1920).
MARGIN_L, MARGIN_R = 60, 200
TEXT_WIDTH = 1080 - MARGIN_L - MARGIN_R
MAX_LINES = 2  # subtitle standards (Netflix, BBC): two lines at most

# Open Font Licence fonts; files are fetched into fonts/ the first time they are needed.
_GF = "https://github.com/google/fonts/raw/main/ofl/"
FONTS = {
    "Montserrat ExtraBold": ("Montserrat-ExtraBold.ttf",
                             "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf"),
    "Anton": ("Anton-Regular.ttf", _GF + "anton/Anton-Regular.ttf"),
    "Bebas Neue": ("BebasNeue-Regular.ttf", _GF + "bebasneue/BebasNeue-Regular.ttf"),
    "Archivo Black": ("ArchivoBlack-Regular.ttf", _GF + "archivoblack/ArchivoBlack-Regular.ttf"),
    "Poppins ExtraBold": ("Poppins-ExtraBold.ttf", _GF + "poppins/Poppins-ExtraBold.ttf"),
}
# Average letter width as a share of the font size, for line-length estimates without the file.
WIDTH_FACTOR = {"Montserrat ExtraBold": 0.62, "Anton": 0.47, "Bebas Neue": 0.43,
                "Archivo Black": 0.66, "Poppins ExtraBold": 0.62}

_BASE = {
    "classic": ("Montserrat ExtraBold", 88, 104),
    "anton": ("Anton", 100, 116),
    "bebas": ("Bebas Neue", 112, 128),
    "archivo": ("Archivo Black", 80, 94),
    "poppins": ("Poppins ExtraBold", 84, 98),
}
LOOKS: dict[str, dict] = {}
for _name, (_font, _size, _hook) in _BASE.items():
    LOOKS[_name] = {"font": _font, "font_size": _size, "hook_font_size": _hook, "box": False, "outline": 6}
    LOOKS[_name + "_box"] = {"font": _font, "font_size": _size - 6, "hook_font_size": _hook - 6,
                             "box": True, "outline": 16}
LOOK_NAMES = list(LOOKS)
LABELS = {k: f"{v['font']}, {'dark box' if v['box'] else 'outline'}" for k, v in LOOKS.items()}


def resolve(style: dict) -> dict:
    """The style render and script use: the channel's look (font, size, outline or box) over its
    own [style] values, the fixed yellow highlight, and the safe-zone margins. look = "custom"
    keeps the channel's own font settings."""
    out = dict(style)
    look = LOOKS.get(style.get("look") or "classic")
    if look and style.get("look") != "custom":
        out.update(look)
    out.setdefault("box", False)
    out["highlight_color"] = HIGHLIGHT
    return out


_tried: set[str] = set()  # one download attempt per font per run


def ensure_fonts(names: set[str] | None = None) -> None:
    """Download missing font files (best effort: libass falls back to another font when one is
    missing, so a failed download never stops a render)."""
    FONTS_DIR.mkdir(exist_ok=True)
    for name, (file, url) in FONTS.items():
        if names is not None and name not in names:
            continue
        path = FONTS_DIR / file
        if path.exists() or name in _tried:
            continue
        _tried.add(name)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
            log.info("downloaded font %s", file)
        except Exception as e:  # noqa: BLE001 - offline or blocked: keep going
            log.warning("could not download font %s: %s", file, e)


def _measurer(style: dict):
    """Width in pixels of a piece of text in this style's font (PIL when the file is there)."""
    font, size = style.get("font", "Montserrat ExtraBold"), int(style.get("font_size", 88))
    file = FONTS_DIR / FONTS.get(font, ("", ""))[0]
    if file.is_file():
        try:
            from PIL import ImageFont
            f = ImageFont.truetype(str(file), size)
            return lambda s: f.getlength(s)
        except Exception:  # noqa: BLE001 - no PIL or a bad file: estimate instead
            pass
    per_char = size * WIDTH_FACTOR.get(font, 0.68)
    return lambda s: len(s) * per_char


def line_count(text: str, style: dict, width: int = TEXT_WIDTH) -> int:
    """Lines a caption block wraps to (greedy, like libass's smart wrapping)."""
    measure = _measurer(style)
    lines, cur = 0, ""
    for w in text.replace("*", "").split():
        trial = f"{cur} {w}".strip()
        if cur and measure(trial) > width:
            lines, cur = lines + 1, w
        else:
            cur = trial
    return lines + (1 if cur else 0)


def assign_missing(settings, channels: list) -> None:
    """Give every channel without a look the first one no other channel uses, and save it in its
    config, so a channel keeps its look when channels are added later."""
    from . import channels as chans, tomlw
    from .config import list_channels, load_channel
    taken = set()
    for cid in list_channels(settings.config_dir):
        try:
            look = load_channel(cid, settings.config_dir).style.get("look")
        except Exception:  # noqa: BLE001 - a broken config elsewhere must not stop a run
            continue
        if look:
            taken.add(look)
    for ch in channels:
        if ch.style.get("look"):
            continue
        free = [n for n in LOOK_NAMES if n not in taken] or LOOK_NAMES
        ch.style["look"] = free[0]
        taken.add(free[0])
        path = chans.path_for(settings, ch.id)
        # Only settings read from a config folder (load_settings) save; hand-built ones (tests) don't.
        if settings.config_root is not None and path.exists():
            data = chans.raw(settings, ch.id)
            data.setdefault("style", {})["look"] = free[0]
            tomlw.write(path, data, chans.HEADER)
        log.info("%s: caption look %s", ch.id, LABELS[free[0]])
