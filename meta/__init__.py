"""The meta video maker: a separate program next to `shorts`. It imports from `shorts` but never
changes it."""

import os
from pathlib import Path


def config_dir() -> Path | None:
    """The live program's config folder. The job runner runs meta from its own checkout
    (~/meta-run) and points here with META_CONFIG_DIR, so channels and local.toml are found."""
    v = os.environ.get("META_CONFIG_DIR")
    return Path(v) if v else None
