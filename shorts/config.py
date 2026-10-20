"""Settings and per-channel configuration."""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MAX_SHORT_SECONDS = 60
BROAD_TAGS = ("shorts", "viral shorts", "for you")  # tag layer 3 default (shorts/tags.py)
ROOT = Path(__file__).resolve().parent.parent
# SHORTS_CONFIG_DIR / --config-dir: another config folder (tests, or jobs started by the dashboard).
CONFIG_DIR = Path(os.environ.get("SHORTS_CONFIG_DIR") or ROOT / "config")


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class Settings:
    data_dir: Path
    claude_cmd: str = "claude"
    encoder: str = "auto"
    x264_preset: str = "veryfast"     # CPU encoding speed when NVENC is unavailable (medium = old, slower)
    render_zoom: bool = True          # render: slow zooms, hook push-in, punch-ins on highlighted words
    render_beat_cuts: bool = True     # render: move cuts up to 0.15 s to land on the music's beat
    render_word_highlight: bool = True  # voiced Shorts: the word being said lights up in the caption
    render_sfx: bool = True           # render: soft whoosh on cuts, low hit on punch-ins (made by ffmpeg)
    render_grade: bool = True         # render: light colour grade per channel, cleanup of old SD film
    render_speed_ramps: bool = True   # render: nearly still shots without people play at 1.2x
    render_gpu_decode: bool = True    # render: decode sources above 1080p on the GPU (NVDEC) when it works
    scene_detector: str = "fast"      # "fast" = one ffmpeg pass per video; "pyscenedetect" = the old way
    scene_fps: float = 10.0           # frames per second the fast scene detector looks at
    clip_model: str = "ViT-B-32"
    clip_pretrained: str = "laion2b_s34b_b79k"
    default_channel: str = "space"
    footage_root: Path | None = None  # 'footage_dir' in settings: downloaded videos (big); default data_dir/footage
    min_free_gb: float = 20.0         # downloads stop when the footage drive has less free space than this
    restock_below: int = 15           # 'make' downloads more footage when fewer Shorts are left in the library
    segment_seconds: float = 90.0     # one Short per this much video; heroes from one video lie this far apart
    max_shorts_per_video: int = 5     # cap on Shorts from one (long) video
    sibling_gap_days: int = 2         # Shorts from the same video are published at least this many days apart
    restock_per_term: int = 2         # videos per search term in one automatic restock
    upload_quota: int = 100           # uploads per day for the Google Cloud project (all channels)
    channel_uploads_per_day: int = 5  # uploads per channel per 24 hours (YouTube's own per-channel limit)
    broad_tags: list[str] = field(default_factory=lambda: list(BROAD_TAGS))  # tag layer 3, every Short
    music_min_tracks: int = 30        # 'make' fetches CC0 music when the shared library has fewer tracks (0 = never)
    config_root: Path | None = None   # config folder these settings came from; default CONFIG_DIR

    @property
    def config_dir(self) -> Path:
        return self.config_root or CONFIG_DIR

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shorts.db"

    @property
    def footage_dir(self) -> Path:
        return self.footage_root or self.data_dir / "footage"

    @property
    def keyframe_dir(self) -> Path:
        return self.data_dir / "keyframes"

    @property
    def render_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def music_dir(self) -> Path:
        return self.data_dir / "music"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.footage_dir, self.keyframe_dir,
                  self.render_dir, self.music_dir, self.secrets_dir):
            d.mkdir(parents=True, exist_ok=True)


@dataclass
class Channel:
    id: str
    name: str
    language: str
    niche: str
    search_terms: list[str]
    formats: list[dict]
    style: dict
    min_seconds: int = 15
    max_seconds: int = 30
    uploads_per_day: int = 2
    publish_times_utc: list[str] = field(default_factory=lambda: ["15:00"])
    hashtags: list[str] = field(default_factory=list)  # old; niche_tags replaces it (still read as a fallback)
    niche_tags: list[str] = field(default_factory=list)  # tag layer 2 (shorts/tags.py); empty = built-in default
    music_folder: str | None = None  # no longer used: all channels share data_dir/music (shorts/music.py)
    music_terms: list[str] = field(default_factory=list)  # music searches (shorts/music.py); empty = defaults
    voice: str | None = None               # voice-over (shorts/voice.py): Kokoro voice id; empty = text + music only
    voice_speed: float = 1.0
    # Footage categories restock searches for this channel (config/categories.toml). Which videos it
    # uses is decided by its niche (shorts/fit.py), not by these.
    categories: list[str] = field(default_factory=list)
    # Footage sources restock downloads from (shorts/sources); empty = NASA only.
    sources: list[str] = field(default_factory=list)
    youtube_channel_id: str | None = None  # set by 'Connect YouTube'; upload refuses another channel
    auto_run: bool = False                 # daily make + upload (python -m shorts daily)
    hero_query: str | None = None          # what a scroll-stopping first frame looks like (CLIP)
    library_formats: bool = True           # also use shared formats (shorts/formats.py) up to 3 in total
    inspiration: list[str] = field(default_factory=list)  # 2-3 similar channels (@handle / link) to learn from

    def __post_init__(self):
        # Claimed Shorts over a minute can't earn at all, and evidence favours short ones.
        self.max_seconds = min(int(self.max_seconds), MAX_SHORT_SECONDS)
        self.min_seconds = min(int(self.min_seconds), self.max_seconds - 1)

    @property
    def enabled_formats(self) -> list[dict]:
        return [f for f in self.formats if f.get("enabled", True)]

    def format(self, format_id: str) -> dict:
        for f in self.formats:
            if f["id"] == format_id:
                return f
        from .formats import get
        if f := get(format_id):
            return f
        raise KeyError(f"unknown format {format_id!r} for channel {self.id}")


def _dir(config_dir: Path | None) -> Path:
    return Path(config_dir) if config_dir else CONFIG_DIR


def load_settings(config_dir: Path | None = None, **overrides) -> Settings:
    config_dir = _dir(config_dir)
    data = _load_toml(config_dir / "settings.toml")
    local = config_dir / "local.toml"
    if local.exists():
        data.update(_load_toml(local))
    data.update({k: v for k, v in overrides.items() if v is not None})
    data["data_dir"] = Path(data["data_dir"])
    if data.get("footage_dir"):
        data["footage_root"] = Path(data.pop("footage_dir"))
    data.pop("footage_dir", None)
    data["config_root"] = config_dir
    return Settings(**data)


def load_channel(channel_id: str, config_dir: Path | None = None) -> Channel:
    path = _dir(config_dir) / "channels" / f"{channel_id}.toml"
    if not path.exists():
        raise FileNotFoundError(f"no channel config at {path}")
    data = _load_toml(path)
    return Channel(**data)


def list_channels(config_dir: Path | None = None) -> list[str]:
    return sorted(p.stem for p in (_dir(config_dir) / "channels").glob("*.toml"))


def all_channels(config_dir: Path | None = None) -> list[Channel]:
    """Every channel; a broken channel file is logged and skipped, so it can't stop the others."""
    out = []
    for cid in list_channels(config_dir):
        try:
            out.append(load_channel(cid, config_dir))
        except Exception:  # noqa: BLE001
            log.exception("channel %s", cid)
    return out
