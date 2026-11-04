"""Footage collector research: install the open-source tools the research tests into their own
environment (~/research-venv), so the live dashboard's .venv is never changed.

The research venv sees the live program's packages through a .pth file (torch, open_clip, numpy
are not installed twice) and adds only:
    yt-dlp                 piece downloads from archives (Unlicense)
    rapidocr-onnxruntime   text / title-card detection on frames (Apache-2.0)
    piq                    image quality scores: BRISQUE, CLIP-IQA (Apache-2.0)
pyiqa was the plan's first pick, but its licence is non-commercial, so piq replaces it.

    python -m meta.collector.tools      # idempotent: re-running upgrades and re-checks
"""
from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path

VENV = Path.home() / "research-venv"
PACKAGES = ["yt-dlp", "rapidocr-onnxruntime", "piq"]
CHECK = "import cv2, yt_dlp, rapidocr_onnxruntime, piq; from rapidocr_onnxruntime import RapidOCR; RapidOCR(); print('opencv', cv2.__version__); print('yt-dlp', yt_dlp.version.__version__); print('piq', piq.__version__); print('rapidocr ok')"


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, timeout=1800)


def main() -> None:
    live_site = sysconfig.get_paths()["purelib"]  # the live .venv this module runs in
    py = VENV / "bin" / "python"
    if not py.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    site = subprocess.run([str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (Path(site) / "shorts-live.pth").write_text(live_site + "\n", encoding="utf-8")
    run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"])
    run([str(py), "-m", "pip", "install", "-q", "--upgrade", *PACKAGES])
    # RapidOCR pulls in opencv-python, which needs a desktop (libGL) and hides the live venv's
    # headless OpenCV: swap it for the headless build, as deploy/bench.sh does.
    subprocess.run([str(py), "-m", "pip", "uninstall", "-q", "-y", "opencv-python"], timeout=600)
    run([str(py), "-m", "pip", "install", "-q", "--force-reinstall", "--no-deps", "opencv-python-headless>=4.9,<5"])
    run([str(py), "-c", CHECK])
    print("research tools ready in", VENV, flush=True)


if __name__ == "__main__":
    main()
