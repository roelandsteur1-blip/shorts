#!/usr/bin/env bash
# Clip quality bench (shorts/bench) on the server: its own venv (~/bench-venv), so the extra
# models never touch the dashboard's .venv; runs in the background at the lowest CPU priority
# with 4 of the 6 cores, so the daily run keeps going.
#
#   bash deploy/bench.sh smoke     set up (first time) + a 5-10 minute trial run on 4 blocks
#   bash deploy/bench.sh start     the real run (all stages; continues where a stopped run was)
#   bash deploy/bench.sh review    the blind-review clips after a finished run (/bench/review)
#   bash deploy/bench.sh status    how far it is, and the last log lines
#   bash deploy/bench.sh stop
set -euo pipefail
# The Claude CLI (the judge) lives here; ssh and nohup shells don't have it on PATH.
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."
REPO=$(pwd)
VENV="$HOME/bench-venv"
PY="$VENV/bin/python"
DATA=$("$REPO/.venv/bin/python" -c "from shorts.config import load_settings; print(load_settings().data_dir)")
LOG="$DATA/bench/run.log"
mkdir -p "$DATA/bench"

setup() {
  [ -x "$PY" ] && "$PY" -c "import ultralytics, faster_whisper, open_clip" 2>/dev/null && return
  echo "Setting up $VENV (first time only, a few minutes)..."
  python3 -m venv "$VENV"
  "$PY" -m pip install -q --upgrade pip
  if [ "$(uname -m)" = "x86_64" ]; then
    "$PY" -m pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
  else
    "$PY" -m pip install -q torch torchvision
  fi
  "$PY" -m pip install -q requests numpy "opencv-python-headless>=4.9,<5" scenedetect open_clip_torch timm \
    transformers pillow onnxruntime faster-whisper ultralytics
  # Ultralytics pulls in the desktop OpenCV; the headless one must win (no display on a server).
  "$PY" -m pip uninstall -q -y opencv-python 2>/dev/null || true
  "$PY" -m pip install -q --force-reinstall --no-deps "opencv-python-headless>=4.9,<5"
  # CLIP for YOLO-World's words (Ultralytics would otherwise try to install it mid-run).
  "$PY" -m pip install -q "git+https://github.com/ultralytics/CLIP.git" || true
  "$PY" -m pip install -q -e "$REPO" --no-deps
}

running() { pgrep -f -- "-m shorts\.bench" >/dev/null; }

case "${1:-status}" in
  smoke)
    setup
    nice -n 19 "$PY" -m shorts.bench --smoke all 2>&1 | tee "$DATA/bench-smoke.log" | tail -60
    ;;
  start)
    setup
    if running; then echo "Already running. bash deploy/bench.sh status"; exit 0; fi
    nohup nice -n 19 "$PY" -m shorts.bench all >> "$LOG" 2>&1 &
    echo "Started in the background. Progress: bash deploy/bench.sh status"
    ;;
  review)
    # Step 4 after a finished run: the blind-review clips (shorts/bench/chains.py).
    setup
    if running; then echo "Already running. bash deploy/bench.sh status"; exit 0; fi
    nohup nice -n 19 "$PY" -m shorts.bench chains >> "$LOG" 2>&1 &
    echo "Rendering the blind review in the background. Progress: bash deploy/bench.sh status"
    ;;
  status)
    if running; then echo "RUNNING"; else echo "not running"; fi
    [ -x "$PY" ] && "$PY" -m shorts.bench status || true
    [ -f "$LOG" ] && tail -n 8 "$LOG" || true
    ;;
  stop)
    pkill -f -- "-m shorts\.bench" && echo "stopped" || echo "was not running"
    ;;
  *)
    echo "usage: bash deploy/bench.sh smoke|start|review|status|stop"; exit 1
    ;;
esac
