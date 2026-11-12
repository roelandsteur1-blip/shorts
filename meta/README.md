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

## Quiz and ranking Shorts, and all samples in one go

    .venv/bin/python -m meta.quiz --all -n 1       # "Can you name this?" in 3 levels, ~20 s
    .venv/bin/python -m meta.ranking --all -n 1    # "Ranking X by Y", 5 items, ~18 s
    .venv/bin/python -m meta.samples               # text cards + quizzes + rankings, new ones in ~/meta-out/samples.tar.gz

## Updating the live program from the runner (only when Roeland asks)

The runner's `update` job does what the update one-liner does: `~/shorts` to `origin/main`, then
`sudo systemctl restart shorts-dashboard`. Claude queues it in `meta/jobs.toml` only when Roeland
asks for an update; pushing to main alone never updates the live program. The restart needs this
one-time rule (it allows that single command and nothing else), run from the laptop:

    ssh -t -i $HOME\.ssh\contabo shorts@5.189.155.248 "echo 'shorts ALL=(root) NOPASSWD: /usr/bin/systemctl restart shorts-dashboard' | sudo tee /etc/sudoers.d/shorts-update >/dev/null && sudo chmod 440 /etc/sudoers.d/shorts-update && sudo visudo -cf /etc/sudoers.d/shorts-update"

Undo: `sudo rm /etc/sudoers.d/shorts-update`. A restart stops a dashboard job that is running.

## Transcript-first clip Shorts (samples only)

Plan: project file `plan/transcript-first-build-plan.md`. Two steps per kind of source
(part 1 newsreel, 2 NASA, 3 Prelinger, 4 feature films from `config/clip-titles.toml` only):

    .venv/bin/python -m meta.tclips prep --part 1   # pick 3 sources, transcribe (cached in ~/meta-out/transcripts)
    .venv/bin/python -m meta.tclips make --part 1   # moments -> 2 Shorts per source in ~/meta-out/tclips

Runner jobs `tclipsprep` and `tclips` (n = part). Transcripts use NASA's own captions or a human
subtitle file when there is one, else faster-whisper distil-large-v3 from the research venv.
