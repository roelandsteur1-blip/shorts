"""Quiet and Fast: how much of the laptop the pipeline may use, switched by hand on the dashboard.

Every heavy step runs at below-normal priority, so whatever you do on the laptop wins. On top of
that, Quiet uses about half the CPU, renders one Short at a time and (Windows 11) asks for
Efficiency mode: cooler and quieter. Fast uses everything. The mode lives in a small file in the
data folder, so a running job picks up a switch between videos/renders, without restarting.
On battery the pipeline always behaves as Quiet.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MODES = ("quiet", "fast")
DEFAULT = "quiet"

# Windows priority class for jobs; child processes (ffmpeg, workers, claude) inherit it.
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def _mode_file(data_dir: Path) -> Path:
    return Path(data_dir) / "speed_mode.txt"


def get_mode(data_dir: Path) -> str:
    try:
        mode = _mode_file(data_dir).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return DEFAULT
    return mode if mode in MODES else DEFAULT


def set_mode(data_dir: Path, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    _mode_file(data_dir).write_text(mode, encoding="utf-8")


def on_battery() -> bool:
    """True when a laptop runs on battery (Windows only; elsewhere False)."""
    if sys.platform != "win32":
        return False

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    status = SYSTEM_POWER_STATUS()
    try:
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return False
    except (AttributeError, OSError):
        return False
    return status.ACLineStatus == 0


def effective_mode(data_dir: Path) -> str:
    return "quiet" if on_battery() else get_mode(data_dir)


@dataclass
class Limits:
    mode: str
    scene_workers: int   # videos (or parts of long videos) analysed at once
    render_workers: int  # Shorts rendered at once
    downloads: int       # files downloaded at once
    x264_threads: int    # 0 = let x264 decide (all cores)


def limits(data_dir: Path, nvenc: bool = False) -> Limits:
    cores = os.cpu_count() or 4
    if effective_mode(data_dir) == "fast":
        return Limits("fast", scene_workers=cores, render_workers=3 if nvenc else 2,
                      downloads=4, x264_threads=0)
    return Limits("quiet", scene_workers=max(1, cores // 2), render_workers=1,
                  downloads=2, x264_threads=max(2, cores // 2))


def lower_priority(efficiency: bool = False) -> None:
    """Below-normal priority for this process (and the processes it starts). efficiency: also ask
    Windows 11 for Efficiency mode (EcoQoS: slower, cooler, quieter). Never raises."""
    try:
        if sys.platform == "win32":
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
            if efficiency:
                _ecoqos(k32)
        else:
            os.nice(max(0, 5 - os.nice(0)))
    except (AttributeError, OSError) as e:
        log.debug("could not lower priority: %s", e)


def _ecoqos(k32) -> None:
    class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                    ("StateMask", ctypes.c_ulong)]
    PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
    ProcessPowerThrottling = 4
    state = PROCESS_POWER_THROTTLING_STATE(1, PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
                                           PROCESS_POWER_THROTTLING_EXECUTION_SPEED)
    k32.SetProcessInformation(k32.GetCurrentProcess(), ProcessPowerThrottling,
                              ctypes.byref(state), ctypes.sizeof(state))


def worker_init(data_dir: str) -> None:
    """Initializer for worker processes: low priority, Efficiency mode in Quiet."""
    lower_priority(efficiency=effective_mode(Path(data_dir)) == "quiet")


class Gate:
    """Limits how many tasks run at once to the current mode's number, re-read before each new
    task, so switching Quiet/Fast on the dashboard takes effect mid-run."""

    def __init__(self, data_dir: Path, field: str, nvenc: bool = False):
        self.data_dir, self.field, self.nvenc = Path(data_dir), field, nvenc

    def allowed(self) -> int:
        return getattr(limits(self.data_dir, self.nvenc), self.field)
