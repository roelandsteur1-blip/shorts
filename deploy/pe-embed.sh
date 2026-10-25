#!/usr/bin/env bash
# The new footage search (Perception Encoder B/16, 3 frames per scene; project files:
# research/clip-quality.md) needs vectors for the whole library, once: about a day on the server.
# This runs it in the background at the lowest CPU and disk priority with 4 of the 6 cores, so the
# daily run keeps going. A stopped run continues where it was. Search switches over by itself when
# 90% of the library is done; new footage gets its vectors when it comes in.
#
#   bash deploy/pe-embed.sh start
#   bash deploy/pe-embed.sh status
#   bash deploy/pe-embed.sh stop
set -euo pipefail
cd "$(dirname "$0")/.."
PY="$(pwd)/.venv/bin/python"
DATA=$("$PY" -c "from shorts.config import load_settings; print(load_settings().data_dir)")
LOG="$DATA/logs/pe-embed.log"
mkdir -p "$DATA/logs"

running() { pgrep -f -- "-m shorts embed --pe" >/dev/null; }

case "${1:-status}" in
  start)
    if running; then echo "Already running. bash deploy/pe-embed.sh status"; exit 0; fi
    nohup nice -n 19 ionice -c 3 "$PY" -m shorts embed --pe --threads 4 >> "$LOG" 2>&1 &
    echo "Started in the background. Progress: bash deploy/pe-embed.sh status"
    ;;
  status)
    if running; then echo "RUNNING"; else echo "not running"; fi
    "$PY" -m shorts embed --pe-status
    [ -f "$LOG" ] && grep -v '^PROGRESS' "$LOG" | tail -n 4 || true
    ;;
  stop)
    pkill -f -- "-m shorts embed --pe" && echo "stopped" || echo "was not running"
    ;;
  *)
    echo "usage: bash deploy/pe-embed.sh start|status|stop"; exit 1
    ;;
esac
