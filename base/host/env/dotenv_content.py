# meiva:note
# The grammar and byte rendering of a unit's .env. The locked writer supplies
# decoded lines; this owner reads no files, environment, clock or audit resource.

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

_EXPORT_PREFIX = re.compile(r"export[^\S\r\n]+")
_UNQUOTED_KEY = re.compile(r"[^=\s#]+")


# meiva:note
# The key a `.env` assignment line targets, in the parser's own grammar.
#
# `export KEY=v` targets `KEY` — `export` plus whitespace is a prefix, never
# part of the key; `exportKEY=v` keeps its name. `'KEY'=v` is dotenv's
# single-quoted-key form; `"KEY"=v` keeps its quotes, as the parser leaves
# them. Comments, blank lines, and lines that carry no assignment (`KEY`, or
# junk after the key) return `None` — a bare key binds no value, and trailing
# junk makes the parser drop the binding wholesale.
#
# Only the KEY is read here, never the value: a line whose value does not
# decode still reports its key, so a reader can tell "not set" from "set to
# something the parser cannot read" and never silently ignores the latter
# (#2981).
def env_line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    prefix = _EXPORT_PREFIX.match(stripped)
    if prefix is not None:
        stripped = stripped[prefix.end() :]
    if stripped.startswith("'"):
        end = stripped.find("'", 1)
        if end <= 1:
            return None
        key, rest = stripped[1:end], stripped[end + 1 :]
    else:
        unquoted = _UNQUOTED_KEY.match(stripped)
        if unquoted is None:
            return None
        key, rest = unquoted.group(0), stripped[unquoted.end() :]
    rest = rest.lstrip()
    if not rest.startswith("="):
        return None
    return key


# meiva:note
# The normalized `export ` prefix a rewrite of `line` must keep, else `""`.
#
# Purely textual, and only meaningful for a line whose key was matched (the
# writer is about to replace it): a shell consumer treats `export KEY=v` and
# `KEY=v` differently, so a rewrite keeps the operator's prefix instead of
# silently dropping it. Whitespace variants normalize to one space; `exportKEY=`
# and `export=1` carry no prefix.
def env_line_export_prefix(line: str) -> str:
    return "export " if _EXPORT_PREFIX.match(line.lstrip()) else ""


# meiva:note
# Parse `key=value` text into `{alias: value}` for an audit value diff.
#
# Surrounding matching quotes are stripped, matching how the write paths
# render values. This feeds ONLY the local audit record: the audit drops the
# values of sensitive and unregistered aliases before persisting.
def env_values_from_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key = env_line_key(line)
        if key is None:
            continue
        _, _, raw = line.partition("=")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1]
        values[key] = raw
    return values


# meiva:note
# Replace only the first assignment of each updated key, preserving later
# duplicates. Missing keys append in the mapping's insertion order.
def render_env_upsert(lines: Sequence[str], updates: Mapping[str, str]) -> bytes:
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        key = env_line_key(line)
        if key is not None and key in remaining:
            out.append(f"{env_line_export_prefix(line)}{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for k, v in remaining.items():
        out.append(f"{k}={v}")
    return ("\n".join(out) + "\n").encode()


# meiva:note
# None means no assignment matched. Removing every line still renders a final
# newline, exactly like every changing rewrite of the existing writer.
def render_env_removal(lines: Sequence[str], keys: set[str]) -> bytes | None:
    out = [line for line in lines if env_line_key(line) not in keys]
    if len(out) == len(lines):
        return None
    return ("\n".join(out) + "\n").encode()
