# meiva:note
# The closed lists of the ambient-state rule (meizel/state/ambient_state/__init__.py).
#
# Everything the rule lets through without a baseline entry is listed in `meizel.policy.toml`, in
# the `[ambient-state]` tables, and every entry that names a file or a site says why. This module
# only reads them; it holds no entry itself. The lists are closed: a new entry is a deliberate edit
# of the policy with a one-line reason, never an inline comment at the site, and `python -m meizel
# policy diff` reports it. Entries that name a file or a site go stale — the gate fails when the
# file is gone or no longer holds the thing — so a list cannot rot into a permission wall.
#
# The lists are a value, not module state: `AmbientAllowlist` is built once by the composition root
# (`scripts/lint/code_structure.py`, which owns the repo root and the policy read) and handed to the
# rule, so neither this module nor the rule reads a file, and a test builds the lists it wants to
# judge instead of editing globals.
#
# Three families are kept, and only these (user ruling 2026-10-02):
#
# 1. **Write-only facades** — loggers, meters, tracers, and the internals of the
#    log/telemetry sinks. Code writes to them and nothing reads them back to decide
#    what to do (`sink_callees`, `sink_callee_suffixes`, `sink_facades`).
# 2. **Framework wiring** — an `app`/router/parser/graph definition and the
#    synchronization primitives. A lock is wiring; the state it protects is not
#    (`wiring_callees`).
# 3. **Pure constants** — a value computed from literals that cannot change an
#    answer: a compiled regex, a `TypeVar`, a `timedelta`, a `Path`; and a memoized
#    pure function, which is a cache of a constant rather than state
#    (`pure_callees`, `pure_chain_callees`, `pure_repo_callees`, `allowed`).


from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from meizel.allowances import Policy

__all__ = ["AmbientAllowlist", "from_policy"]

_RULE = "ambient-state"


# meiva:note
# The `[ambient-state]` tables as one value: every list is closed, and every entry that names a
# file or a site says why. One field per list, and three facts the names alone do not carry:
# a sink facade's exemption covers module-level state only (a thread or task the facade starts is
# still reported), `allowed` is where a memoized pure function goes (an `lru_cache` over a
# derivation that depends on nothing mutable is a cache of a constant, not state), and every entry
# that names a file or a site goes stale — the gate fails when the file is gone or no longer holds
# the thing.
@dataclass(frozen=True, slots=True)
class AmbientAllowlist:
    skill_script_patterns: tuple[re.Pattern[str], ...]
    config_modules: tuple[str, ...]
    config_readers: frozenset[str]
    settings_names: frozenset[str]
    sink_callees: frozenset[str]
    sink_callee_suffixes: tuple[str, ...]
    sink_facades: Mapping[str, str]
    wiring_callees: frozenset[str]
    pure_callees: frozenset[str]
    pure_chain_callees: frozenset[str]
    pure_repo_callees: Mapping[str, str]
    allowed: Mapping[str, str]


# meiva:note
# The rule's lists read off a policy its caller loaded and owns: this function decides nothing
# about where the policy came from, so the reader that finds the repository stays in the caller.
def from_policy(policy: Policy) -> AmbientAllowlist:
    readers = policy.names(_RULE, "config-readers")
    return AmbientAllowlist(
        skill_script_patterns=tuple(
            re.compile(pattern) for pattern in policy.names(_RULE, "skill-script-patterns")
        ),
        config_modules=policy.names(_RULE, "config-modules"),
        config_readers=frozenset(readers),
        settings_names=frozenset(readers[:1]),
        sink_callees=frozenset(policy.names(_RULE, "sink-callees")),
        sink_callee_suffixes=policy.names(_RULE, "sink-callee-suffixes"),
        sink_facades=policy.reasons(_RULE, "sink-facades"),
        wiring_callees=frozenset(policy.names(_RULE, "wiring-callees")),
        pure_callees=frozenset(policy.names(_RULE, "pure-callees")),
        pure_chain_callees=frozenset(policy.names(_RULE, "pure-chain-callees")),
        pure_repo_callees=policy.reasons(_RULE, "pure-repo-callees"),
        allowed=policy.reasons(_RULE, "sites"),
    )
