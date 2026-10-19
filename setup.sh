#!/usr/bin/env bash
# One-time setup on a Linux server (Ubuntu 22.04 / 24.04 / 26.04, x86 or Arm). Run from the repo folder:
#   bash setup.sh
# Installs system packages, Python packages (CPU-only PyTorch), the font, Claude Code, Tailscale,
# the always-on dashboard service and a nightly database backup. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
DATA="${SHORTS_DATA:-$HOME/shorts-data}"

echo "== System packages"
sudo apt-get update -q
sudo apt-get install -y -q python3-venv python3-dev build-essential ffmpeg espeak-ng sqlite3 cron curl git

echo "== Python 3.11 or 3.12 (Kokoro doesn't support 3.13+ yet; other versions: uv fetches 3.12)"
if python3 -c 'import sys; sys.exit(not (3, 11) <= sys.version_info[:2] <= (3, 12))'; then
  MKVENV="python3 -m venv .venv"
else
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  uv python install 3.12
  MKVENV="uv venv --seed --python 3.12 .venv"
fi

echo "== Python virtual environment"
[ -d .venv ] || $MKVENV
PY="$REPO/.venv/bin/python"
"$PY" -m pip install -q --upgrade pip

echo "== PyTorch (CPU only, no GPU on the server)"
if [ "$(uname -m)" = "x86_64" ]; then
  "$PY" -m pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
else
  "$PY" -m pip install -q torch torchvision   # Arm: the normal wheels are CPU-only
fi

echo "== Other packages"
"$PY" -m pip install -q -r requirements.txt
"$PY" -m pip install -q -e .

echo "== Font (Montserrat ExtraBold, open font licence)"
mkdir -p fonts
[ -f fonts/Montserrat-ExtraBold.ttf ] || curl -fsSL -o fonts/Montserrat-ExtraBold.ttf \
  https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf

echo "== Settings for the server (config/local.toml)"
if [ ! -f config/local.toml ]; then
  cat > config/local.toml <<TOML
# Server settings (written by setup.sh)
data_dir = "$DATA"
footage_dir = "$DATA/footage"
encoder = "libx264"          # no NVIDIA GPU here
render_gpu_decode = false
min_free_gb = 25             # cleanup keeps the 200 GB disk from filling up
TOML
fi
mkdir -p "$DATA/secrets" "$DATA/logs"
chmod 700 "$DATA/secrets"

echo "== Claude Code"
command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash

echo "== Tailscale (private access to the dashboard)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh

echo "== Dashboard as an always-on service"
sed -e "s|@USER@|$USER|g" -e "s|@REPO@|$REPO|g" -e "s|@PATH@|$HOME/.local/bin:$PATH|g" \
  deploy/shorts-dashboard.service | sudo tee /etc/systemd/system/shorts-dashboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now shorts-dashboard

echo "== Nightly database backup (03:30, keeps 7)"
( crontab -l 2>/dev/null | grep -v '# shorts-backup' || true
  echo "30 3 * * * cd \"$REPO\" && \"$PY\" -m shorts backup >> \"$DATA/logs/backup.log\" 2>&1 # shorts-backup"
) | crontab -

"$PY" -m shorts check || true
cat <<MSG

Done. Still to do once (see deploy/README.md):
  1. claude setup-token   -> put CLAUDE_CODE_OAUTH_TOKEN=<token> in $DATA/secrets/server.env
  2. sudo tailscale up     -> then: sudo tailscale serve --bg 5000
  3. copy the laptop's data over and run 'rebase-paths'
MSG
