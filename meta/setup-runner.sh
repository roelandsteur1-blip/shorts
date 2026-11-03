#!/usr/bin/env bash
# One-time setup of the meta job runner (see meta/runner.py). Run as the `shorts` user; no sudo.
# It adds: a separate checkout ~/meta-run (the live ~/shorts is not touched), a deploy key for the
# private results repo, and one cron line. Undo: `crontab -l | grep -v '# meta-runner' | crontab -`
set -euo pipefail
cd "$HOME/shorts"
git fetch -q origin main
if [ ! -d "$HOME/meta-run" ]; then
  git worktree add -q --detach "$HOME/meta-run" origin/main
fi
mkdir -p "$HOME/.ssh" "$HOME/meta-out"
[ -f "$HOME/.ssh/meta_results_deploy" ] || ssh-keygen -q -t ed25519 -N "" -C "shorts server results" -f "$HOME/.ssh/meta_results_deploy"
LINE="*/5 * * * * flock -n \$HOME/meta-runner.lock sh -c 'cd \$HOME/meta-run && git fetch -q origin main && git reset -q --hard origin/main && META_CONFIG_DIR=\$HOME/shorts/config \$HOME/shorts/.venv/bin/python -m meta.runner >> \$HOME/meta-out/runner.log 2>&1' # meta-runner"
( crontab -l 2>/dev/null | grep -v '# meta-runner' || true; echo "$LINE" ) | crontab -
echo
echo "Runner installed (every 5 minutes). Add this key on GitHub to the PRIVATE repo Shorts_results:"
echo "Settings -> Deploy keys -> Add deploy key, tick 'Allow write access':"
echo
cat "$HOME/.ssh/meta_results_deploy.pub"
