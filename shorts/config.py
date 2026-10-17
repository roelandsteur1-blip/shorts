"""Settings and per-channel configuration."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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

    def ensure_channel_dirs(self, channel_ids: list[str]) -> None:
        for cid in channel_ids:
            (self.music_dir / cid).mkdir(parents=True, exist_ok=True)

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
    hashtags: list[str] = field(default_factory=list)
    music_folder: str | None = None  # subfolder of data_dir/music; default: channel id
    # Footage categories this channel uses (config/categories.toml); empty = every category.
    categories: list[str] = field(default_factory=list)
    # Footage sources restock downloads from (shorts/sources); empty = NASA only.
    sources: list[str] = field(default_factory=list)
    youtube_channel_id: str | None = None  # set by 'Connect YouTube'; upload refuses another channel
    auto_run: bool = False                 # daily make + upload (python -m shorts daily)
    hero_query: str | None = None          # what a scroll-stopping first frame looks like (CLIP)

    @property
    def enabled_formats(self) -> list[dict]:
        return [f for f in self.formats if f.get("enabled", True)]

    def format(self, format_id: str) -> dict:
        for f in self.formats:
            if f["id"] == format_id:
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
