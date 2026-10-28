# meiva:note
# What the `turn_scoped_config` rule decides, over the values its shell read: which scanned files the
# rule skips, which of a file's lines read a per-agent field through a process global, and the text
# of each of the three report headings. The shell walks the scan directories, resolves and reads
# each file, and prints.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

import re
from collections.abc import Collection

__all__ = [
    "LAYERED_HEADING",
    "PLUGIN_HEADING",
    "SINGLETON_HEADING",
    "layered_reads",
    "plugin_config_reads",
    "settings_reads",
    "skips",
]

_SETTINGS_ATTR = re.compile(r"\bsettings\.([a-z_]+)\.([a-z_]+)")
# meiva:note
# A per-agent field read through the model layering or `get_field` by its literal name; the first
# group is the field, the second the rest of the call (to see whether it names `overrides`).
_LAYERED_READ = re.compile(r"\b(?:resolve_setting|get_field)\(\s*[\"']([a-z_]+)[\"']([^)]*)\)")
# meiva:note
# Subscript only — `name in _PLUGIN_CONFIGS` is a registration probe, not a read.
_PLUGIN_CONFIGS_READ = re.compile(r"\b_PLUGIN_CONFIGS\[")

PLUGIN_HEADING = (
    "plugin config read straight out of the process-global _PLUGIN_CONFIGS in turn-scoped "
    "code — use `get_plugin_config(<plugin>, slices)` (or `process_plugin_config` in the "
    "exec child); in hosted mode the map holds whichever agent booted the process:"
)
LAYERED_HEADING = (
    "per-agent setting resolved from the cluster value in turn-scoped code — pass the "
    "agent's `overrides` slice to `resolve_setting`, and read other per-agent fields "
    "from the slices:"
)
SINGLETON_HEADING = (
    "per-agent config read through the bare settings singleton in turn-scoped code — read "
    "it from the agent's slices or `agent_setting`; in hosted mode the singleton holds "
    "the CLUSTER default, not this agent's overlay:"
)


# meiva:note
# Whether the rule skips a file: an exempt path, anything under a tests directory, and a test
# module — judged by the name the scan reached, not by the one a link resolves to.
def skips(rel: str, name: str, *, allowed_files: Collection[str]) -> bool:
    return (
        rel in allowed_files
        or "/tests/" in rel
        or rel.startswith("tests/")
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


# meiva:note
# `file:line: call` for each per-agent setting read by name without the agent's overrides.
def layered_reads(text: str, rel: str, per_agent: Collection[str]) -> list[str]:
    return [
        f"{rel}:{text.count(chr(10), 0, match.start()) + 1}: {match.group(0).splitlines()[0].strip()}"
        for match in _LAYERED_READ.finditer(text)
        if match.group(1) in per_agent and "overrides" not in match.group(2)
    ]


# meiva:note
# `file:line: line` for each per-agent field read through the bare settings singleton, in line
# order; a commented-out read is not code.
def settings_reads(text: str, rel: str, per_agent: Collection[str]) -> list[str]:
    return [
        f"{rel}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if not line.lstrip().startswith("#")
        for match in _SETTINGS_ATTR.finditer(line)
        if match.group(2) in per_agent
    ]


# meiva:note
# `file:line: line` for each subscript read of the process-global plugin map, in line order, for a
# file that is not the mechanism itself.
def plugin_config_reads(text: str, rel: str, mechanism_files: Collection[str]) -> list[str]:
    if rel in mechanism_files:
        return []
    return [
        f"{rel}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if not line.lstrip().startswith("#") and _PLUGIN_CONFIGS_READ.search(line)
    ]
