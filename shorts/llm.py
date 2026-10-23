"""Call Claude through Claude Code headless (`claude -p`), using the Claude subscription.

Used for topics, scripts and titles. The script writer also gets small contact sheets of the
footage (images in the same call), so it can write about and pick clips it has actually seen.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time

log = logging.getLogger(__name__)


# Never a console window for a Claude call (the dashboard runs without a console).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LLMError(RuntimeError):
    pass


def find_claude(claude_cmd: str = "claude") -> str | None:
    """The configured Claude Code command, or None when it isn't installed."""
    return shutil.which(claude_cmd)


class LLMLimitError(LLMError):
    """The Claude subscription's usage limit is reached: stop calling until it resets."""


# Hit once in a run: the rest of that run skips Claude instead of failing call after call.
_limit_hit: str | None = None

SYSTEM_PROMPT = ("You write on-screen text for short factual videos. "
                 "Answer with only the JSON the user asks for. You have no tools.")

# Flags that strip Claude Code's own coding context (tools, MCP servers, skills, project
# settings, CLAUDE.md) from every call: fewer tokens against the Pro limit and a faster start.
# Not --bare: that skips the subscription login and only works with an API key.
LEAN_FLAGS = [("--tools", ""), ("--strict-mcp-config", None), ("--setting-sources", "user"),
              ("--disable-slash-commands", None), ("--no-session-persistence", None),
              ("--system-prompt", SYSTEM_PROMPT)]

_flag_cache: dict[str, list[str]] = {}


def lean_flags(exe: str) -> list[str]:
    """LEAN_FLAGS this Claude Code version knows (checked once via --help), so an older or newer
    version without one of them still works."""
    if exe not in _flag_cache:
        try:
            help_text = subprocess.run([exe, "--help"], capture_output=True, text=True, encoding="utf-8",
                                       errors="replace", timeout=60, creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.TimeoutExpired):
            help_text = ""
        flags: list[str] = []
        for flag, value in LEAN_FLAGS:
            if re.search(re.escape(flag) + r"(?=[\s,=<]|$)", help_text, re.M):
                flags += [flag] if value is None else [flag, value]
        _flag_cache[exe] = flags
        log.debug("claude flags: %s", flags)
    return _flag_cache[exe]


LIMIT_PATTERN = re.compile(r"usage limit|limit reached|rate.?limit|out of (extra )?usage|resets? (at|in)", re.I)


def image_message(prompt: str, images: list[bytes]) -> str:
    """One stream-json user message: the prompt plus JPEG images, in that order."""
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(img).decode("ascii")}} for img in images]
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def result_envelope(stdout: str) -> dict | None:
    """The final result object of a stream-json run (the same fields as --output-format json)."""
    for line in reversed(stdout.splitlines()):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None


def ask(prompt: str, claude_cmd: str = "claude", timeout: int = 600, conn=None, stage: str = "claude",
        images: list[bytes] | None = None) -> str:
    """images: JPEGs sent along with the prompt (Claude Code takes images only as stream-json)."""
    global _limit_hit
    if _limit_hit:
        raise LLMLimitError(_limit_hit)
    exe = find_claude(claude_cmd) or claude_cmd
    from . import timing
    t0 = time.perf_counter()
    if images:
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               *lean_flags(exe)]
        stdin = image_message(prompt, images)
    else:
        cmd, stdin = [exe, "-p", "--output-format", "json", *lean_flags(exe)], prompt
    # Run in an empty folder so Claude Code doesn't pick up this repo's files as context.
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(
                cmd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, cwd=cwd, creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude did not answer within {timeout}s") from e
    if images:
        envelope = result_envelope(proc.stdout)
    else:
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = None
    message = (str(envelope.get("result")) if isinstance(envelope, dict) else "") + " " + proc.stderr
    if proc.returncode != 0 or envelope is None or envelope.get("is_error"):
        if LIMIT_PATTERN.search(message):
            _limit_hit = f"Claude usage limit reached: {message.strip()[:300]}"
            log.warning("%s. Claude steps pause; everything already written still renders, and the "
                        "rest continues on the next run after the limit resets.", _limit_hit)
            raise LLMLimitError(_limit_hit)
    if proc.returncode != 0:
        raise LLMError(f"claude exited with {proc.returncode}: {proc.stderr.strip()[-1000:]}")
    if envelope is None:
        raise LLMError(f"unexpected output from claude: {proc.stdout[:500]}")
    if envelope.get("is_error"):
        raise LLMError(f"claude reported an error: {envelope.get('result')}")
    timing.record(conn, stage, time.perf_counter() - t0, usage=envelope.get("usage") or {},
                  cost_usd=envelope.get("total_cost_usd"), prompt_chars=len(prompt), images=len(images or []))
    return envelope.get("result", "")


def limit_reached() -> bool:
    return _limit_hit is not None


def reset_limit() -> None:
    """Call Claude again (a long-running job that waited for the limit to reset)."""
    global _limit_hit
    _limit_hit = None


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


def ask_json(prompt: str, claude_cmd: str = "claude", retries: int = 1, conn=None, stage: str = "claude",
             images: list[bytes] | None = None):
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return extract_json(_ask(prompt, claude_cmd, conn, stage, images))
        except LLMLimitError:
            raise
        except (LLMError, json.JSONDecodeError) as e:
            last = e
            log.warning("claude call failed (attempt %d): %s", attempt + 1, e)
    raise LLMError(str(last))


def _ask(prompt: str, claude_cmd: str, conn, stage: str, images: list[bytes] | None = None) -> str:
    """ask() with timing info and images when it is the real one (tests replace ask with a 2-3
    argument fake)."""
    if ask.__module__ == __name__ and getattr(ask, "__name__", "") == "ask":
        return ask(prompt, claude_cmd, conn=conn, stage=stage, images=images)
    return ask(prompt, claude_cmd)
