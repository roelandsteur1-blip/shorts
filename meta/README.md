# meta: the meta video maker (in progress)

A separate program next to `shorts`. It may import from `shorts`, but never changes its code,
settings, database, channels or schedule.

Plan: project file `plan/youtube-meta-research-plan.md`.

## Step 1: scan which Shorts win right now (read-only)

    cd ~/shorts && .venv/bin/python -m meta.scan

Uses about 4,000 YouTube API units (30 searches at 100 + reading ~250 channels), writes
`~/meta-scan/` and `~/meta-scan.tar.gz`. `--budget N` caps the units, `--no-extras` skips cover
images and captions. Captions need `yt-dlp` in the venv (`.venv/bin/pip install yt-dlp`).

## Phase 1: text-card Shorts (samples only, no uploads)

    cd ~/shorts && .venv/bin/python -m meta.textcard --all -n 2

One Claude call per niche, footage from the shorts library (read-only), output in
`~/meta-out/textcard/` and `~/meta-out/textcard.tar.gz`.
