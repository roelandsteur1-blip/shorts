"""Call Claude through Claude Code headless (`claude -p`), using the Claude subscription.

Only used for topics, scripts, fact checks and titles; never for describing footage.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def find_claude(claude_cmd: str = "claude") -> str | None:
    """The configured command, else the newest Claude Code bundled with the Claude desktop app.
    That bundled path contains a version number, so it changes whenever the app updates."""
    found = shutil.which(claude_cmd)
    if found:
        return found
    appdata = os.environ.get("APPDATA")
    if appdata:
        bundled = sorted(Path(appdata, "Claude", "claude-code").glob("*/*/claude.exe"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if bundled:
            return str(bundled[0])
    return None


def ask(prompt: str, claude_cmd: str = "claude", timeout: int = 600) -> str:
    exe = find_claude(claude_cmd) or claude_cmd
    # Run in an empty folder so Claude Code doesn't pick up this repo's files as context.
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(
                [exe, "-p", "--output-format", "json"],
                input=prompt, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, cwd=cwd,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude did not answer within {timeout}s") from e
    if proc.returncode != 0:
        raise LLMError(f"claude exited with {proc.returncode}: {proc.stderr.strip()[-1000:]}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"unexpected output from claude: {proc.stdout[:500]}") from e
    if envelope.get("is_error"):
        raise LLMError(f"claude reported an error: {envelope.get('result')}")
    return envelope.get("result", "")


def extract_json(text: str):
    """Parse the first JSON object/array in a reply (Claude sometimes wraps it in ```json)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise LLMError(f"no JSON in reply: {text[:300]}")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    return obj


def ask_json(prompt: str, claude_cmd: str = "claude", retries: int = 1):
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return extract_json(ask(prompt, claude_cmd))
        except (LLMError, json.JSONDecodeError) as e:
            last = e
            log.warning("claude call failed (attempt %d): %s", attempt + 1, e)
    raise LLMError(str(last))
