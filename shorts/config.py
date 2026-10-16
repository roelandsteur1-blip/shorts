"""Settings and per-channel configuration."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class Settings:
    data_dir: Path
    claude_cmd: str = "claude"
    encoder: str = "auto"
    clip_model: str = "ViT-B-32"
    clip_pretrained: str = "laion2b_s34b_b79k"
    default_channel: str = "space"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shorts.db"

    @property
    def footage_dir(self) -> Path:
        return self.data_dir / "footage"

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

    @property
    def enabled_formats(self) -> list[dict]:
        return [f for f in self.formats if f.get("enabled", True)]

    def format(self, format_id: str) -> dict:
        for f in self.formats:
            if f["id"] == format_id:
                return f
        raise KeyError(f"unknown format {format_id!r} for channel {self.id}")


def load_settings(config_dir: Path = CONFIG_DIR, **overrides) -> Settings:
    data = _load_toml(config_dir / "settings.toml")
    local = config_dir / "local.toml"
    if local.exists():
        data.update(_load_toml(local))
    data.update({k: v for k, v in overrides.items() if v is not None})
    data["data_dir"] = Path(data["data_dir"])
    return Settings(**data)


def load_channel(channel_id: str, config_dir: Path = CONFIG_DIR) -> Channel:
    path = config_dir / "channels" / f"{channel_id}.toml"
    if not path.exists():
        raise FileNotFoundError(f"no channel config at {path}")
    data = _load_toml(path)
    return Channel(**data)


def list_channels(config_dir: Path = CONFIG_DIR) -> list[str]:
    return sorted(p.stem for p in (config_dir / "channels").glob("*.toml"))
