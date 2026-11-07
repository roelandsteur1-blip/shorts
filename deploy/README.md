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
`SERVER` below is `shorts@<IP>`. `setup.sh` then turns password and root login off and enables a
firewall (only SSH and Tailscale), fail2ban and automatic security updates: a fresh VPS with
password login on gets broken into within hours. Use a long random root password at the provider anyway.

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

## Daily use

- Dashboard: the Tailscale address.
- Logs: `journalctl -u shorts-dashboard -f`, job logs in `~/shorts-data/logs`.
- Update the code: `cd ~/shorts && git fetch origin main && git reset --hard origin/main && sudo systemctl restart shorts-dashboard`
  (running jobs keep going and the restarted dashboard picks them up again; needs KillMode=process
  in the installed unit, see "Jobs through restarts" below).
- Backups: every night to `~/shorts-data/backups` (7 kept). Copy one to the laptop now and then:
  `scp -i KEY SERVER:shorts-data/backups/<newest>.db D:\shorts-backups\`.
- Connect a YouTube channel: run `ssh -i KEY -L 8765:localhost:8765 SERVER` on the laptop, click
  Connect YouTube on the dashboard and open the login link from the job page in the laptop's browser.

## New footage search (once)

The footage search uses Perception Encoder B/16 on 3 frames per scene (the clip-quality research's
winner, project files: research/clip-quality.md). The library needs its vectors once, about a day
on this server at the lowest CPU priority; search switches over by itself at 90%, and new footage
gets its vectors when it comes in. The Library page shows how far it is.

```bash
bash deploy/pe-embed.sh start     # continues where a stopped run was
bash deploy/pe-embed.sh status
```

Turn it off with `search_pe = false` in settings (CLIP search as before).

## Clip quality bench (research, optional)

`shorts/bench` measures which ways of finding, timing and cropping clips work best (plan in the
project files: plan/clip-quality-research-plan.md). It has its own venv (`~/bench-venv`), works on
a copy of the database and runs at the lowest CPU priority on 4 cores; the judge (Claude) waits
4 minutes between calls so the daily run keeps enough of the Pro limit.

```bash
bash deploy/bench.sh smoke    # first time: sets up the venv, then a short trial run
bash deploy/bench.sh start    # the real run in the background (continues where it stopped)
bash deploy/bench.sh status
```

Progress and results also show on the dashboard at `/bench`, with a page to check Claude's labels.
Results: `~/shorts-data/bench/report.md`.

## Upload via phone (trending sounds in the YouTube app)

For channels set to **Upload via phone** (channel Settings, "How Shorts get to YouTube"), Shorts are
made without music and go to Google Drive instead of YouTube (`shorts/drive.py`, background in
project files: research/app-music-workflow.md). Only one channel's Shorts are in the folder at a
time; Home shows "On your phone now: <channel>". When they are all posted, the next channel's
approved Shorts follow within 30 minutes.

One-time setup:
1. Google Cloud console, project `shorts-pipeline`: enable the **Google Drive API**.
2. Laptop: `ssh -i KEY -L 8765:localhost:8765 SERVER`, then on a phone channel's Settings page press
   **Connect Google Drive** and open the login link from the job page in the laptop's browser.
   One login covers all channels; the app only sees files it made itself (scope drive.file).
3. With the same tunnel, press **Connect again** under YouTube on every phone channel: the server
   now also asks permission to change the channel's videos (to set title, description, tags and time).
4. Phone: install **Autosync for Google Drive** (MetaCtrl) and sync `Shorts for phone` two-way.

Posting: switch to the channel Home names, then for each Short in the gallery: +, Short, pick it,
Add sound, title = **the first 3 words on screen**, visibility **Private**, Upload. Within 30
minutes the server finds it (by those words, or by its length when there's a typo), sets the real
title, description, tags and publish time, and trashes the Drive file, so Autosync removes it from
the phone. "Check now" on the channel's Settings page does it right away.
