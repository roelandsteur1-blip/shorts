# Running everything on a Linux server

The dashboard and all its jobs run 24/7 on a small server (target: Contabo Cloud VPS 20, 6 vCPU /
12 GB / 200 GB SSD, Ubuntu 24.04; Oracle Always Free Ampere A1 works too). No GPU: rendering uses libx264,
CLIP / CLAP / Kokoro run on the CPU. The dashboard stays on 127.0.0.1; Tailscale makes it reachable
from your own devices only.

In the commands below `KEY` is your SSH private key on the laptop (e.g. `C:\Users\roela\.ssh\contabo`,
made with `ssh-keygen -t ed25519 -f $HOME\.ssh\contabo`).

## 1. Install (on the server)

Contabo logs you in as `root` with a password. Make a normal user once and give it your key:

```bash
ssh root@<IP>
adduser --disabled-password --gecos "" shorts && usermod -aG sudo shorts
echo "shorts ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/shorts
mkdir -p /home/shorts/.ssh && cp ~/.ssh/authorized_keys /home/shorts/.ssh/ 2>/dev/null
chown -R shorts:shorts /home/shorts/.ssh
```

(Add your laptop's public key to `/home/shorts/.ssh/authorized_keys` if it isn't there yet.) Then
`SERVER` below is `shorts@<IP>`.

```bash
ssh -i KEY SERVER
git clone https://github.com/roelandsteur1-blip/shorts.git && cd shorts
bash setup.sh
```

## 2. Claude

On the server run `claude setup-token`, open the link it prints on your laptop, and put the token
it gives you in `~/shorts-data/secrets/server.env`:

```
CLAUDE_CODE_OAUTH_TOKEN=<token>
```

Every `python -m shorts` command reads that file, so the dashboard, its jobs and the daily cron
task all use your Claude subscription. Check with `python -m shorts check`.

## 3. Private access (Tailscale)

```bash
sudo tailscale up            # log in with the link it prints
sudo tailscale serve --bg 5000
```

Install Tailscale on the laptop and phone with the same account; the dashboard is then at
`https://<server name>.<tailnet>.ts.net`. Nothing is opened to the internet.

## 4. Move the data from the laptop (PowerShell on the laptop)

Stop the dashboard on the laptop first, so no job writes while copying.

```powershell
cd D:\shorts
ssh -i KEY SERVER "sudo systemctl stop shorts-dashboard; rm -f shorts-data/shorts.db-wal shorts-data/shorts.db-shm"
.\.venv\Scripts\python -m shorts backup --to D:\shorts-transfer\shorts.db
scp -i KEY D:\shorts-transfer\shorts.db SERVER:shorts-data/
scp -i KEY D:\shorts-data\sources.json SERVER:shorts-data/
scp -i KEY -r D:\shorts-data\secrets D:\shorts-data\music D:\shorts-data\keyframes D:\shorts-data\renders SERVER:shorts-data/
scp -i KEY -r D:\shorts\config\channels SERVER:shorts/config/
scp -i KEY -r D:\shorts-footage SERVER:      # ~45 GB, takes hours
```

Channel settings other than `space.toml` are not in git, so the `config/channels` copy matters.
`secrets` holds the YouTube logins (they keep working on the server) and API keys.

Then on the server, point the stored paths at the new folders and restart:

```bash
mv ~/shorts-footage/* ~/shorts-data/footage/ && rmdir ~/shorts-footage
cd ~/shorts
.venv/bin/python -m shorts rebase-paths "D:\shorts-footage=$HOME/shorts-data/footage" "D:\shorts-data=$HOME/shorts-data"
sudo systemctl start shorts-dashboard
```

## Daily use

- Dashboard: the Tailscale address. The Schedule page's daily task becomes a cron line (server
  time, UTC on Oracle).
- Logs: `journalctl -u shorts-dashboard -f`, job logs in `~/shorts-data/logs`.
- Update the code: `cd ~/shorts && git pull && sudo systemctl restart shorts-dashboard`
  (running jobs stop and show as interrupted).
- Backups: every night to `~/shorts-data/backups` (7 kept). Copy one to the laptop now and then:
  `scp -i KEY SERVER:shorts-data/backups/<newest>.db D:\shorts-backups\`.
- Reconnect a YouTube channel: easiest on the laptop, then copy `secrets\token-<channel>.json` to
  the server. Or run `ssh -i KEY -L 8765:localhost:8765 SERVER` and click Connect YouTube on the
  dashboard; open the link from the job log in the laptop's browser.
