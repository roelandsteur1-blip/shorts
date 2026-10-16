"""Write the small TOML files the dashboard edits (channels, categories). tomllib only reads.

Handles what those files use: strings, numbers, booleans, lists of those, tables and arrays of
tables. Comments in a rewritten file are not kept (the old file is saved as .bak first)."""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        if "\n" in v and '"""' not in v and "\\" not in v:
            return '"""\n' + v + '"""'
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        items = [_value(x) for x in v]
        one_line = "[" + ", ".join(items) + "]"
        if len(one_line) <= 100:
            return one_line
        return "[\n" + "".join(f"  {x},\n" for x in items) + "]"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def _is_table_array(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _table(data: dict, prefix: str, out: list[str]) -> None:
    simple = {k: v for k, v in data.items() if not isinstance(v, dict) and not _is_table_array(v)}
    for k, v in simple.items():
        if v is not None:
            out.append(f"{_key(k)} = {_value(v)}")
    for k, v in data.items():
        name = f"{prefix}{_key(k)}"
        if isinstance(v, dict):
            out.append(f"\n[{name}]")
            _table(v, name + ".", out)
        elif _is_table_array(v):
            for item in v:
                out.append(f"\n[[{name}]]")
                _table(item, name + ".", out)


def dumps(data: dict) -> str:
    out: list[str] = []
    _table(data, "", out)
    return "\n".join(out).strip() + "\n"


def write(path: Path, data: dict, header: str = "") -> None:
    """Validate by reading back, keep the old file as .bak, then replace atomically."""
    text = (header.rstrip() + "\n" if header else "") + dumps(data)
    if tomllib.loads(text) != _without_none(data):
        raise ValueError("TOML round trip changed the data")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_bytes(path.read_bytes())
    os.replace(tmp, path)


def _without_none(v):
    if isinstance(v, dict):
        return {k: _without_none(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_without_none(x) for x in v]
    if isinstance(v, tuple):
        return [_without_none(x) for x in v]
    return v
