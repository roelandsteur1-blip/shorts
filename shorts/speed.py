"""How much the pipeline does at once on the server. Heavy steps run at lower priority (nice), so
the dashboard stays quick while a job renders."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limits:
    scene_workers: int   # videos (or parts of long videos) analysed at once
    render_workers: int  # Shorts rendered at once
    downloads: int       # files downloaded at once
    x264_threads: int    # 0 = let x264 decide (all cores)


def limits() -> Limits:
    return Limits(scene_workers=os.cpu_count() or 4, render_workers=2, downloads=4, x264_threads=0)


def lower_priority() -> None:
    """Lower priority for this process (and the processes it starts). Never raises."""
    try:
        os.nice(max(0, 5 - os.nice(0)))
    except (AttributeError, OSError) as e:
        log.debug("could not lower priority: %s", e)


def worker_init(_data_dir: str = "") -> None:
    """Initializer for worker processes."""
    lower_priority()
