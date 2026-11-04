# meiva:note
# What this tool is configured with, and the numbers it judges by. Nothing here reads the
# world: a path is a value, a threshold is a number, and `ProbeUnavailableError` is the one failure
# the reader of another agent's ledger can raise. `scripts/host_ops/docs/worktree-gc.meiva.okf.md`
# owns the reasoning behind every constant below.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # meiva:note
    # Every path is resolved once, before a round starts: one copy of this tool runs against
    # the live home, another against a fixture, and the difference is this value alone.
    repo: Path
    roots: tuple[Path, ...]
    probe: Path | None
    probe_ledger: Path
    hold: Path
    manifests: Path
    ledger: Path
    never: Path
    blind_state: Path
    home_env: Path
    owner: int
    gateway: str


class ProbeUnavailableError(RuntimeError):
    # meiva:note
    # The snapshot judge cannot be read: absent, unreadable, stale, or not JSON. A caller that
    # catches this degrades to the moment-of-deletion gate; it never reads as "nothing to do".
    pass


# meiva:note
# The disk gate. Two independent readings of the same fact, either one opens it: fewer than
# AVAIL_TRIGGER_GIB free, or the volume at or above USED_PCT_TRIGGER. Above both, a run
# reclaims nothing and says so (no churn, no surprises).
AVAIL_TRIGGER_GIB = 30.0
USED_PCT_TRIGGER = 85.0
MAX_PER_RUN = 5

# meiva:note
# Two cool-offs on the clocks that actually move, because "the tree is older than an hour" is
# neither of them. 2026-10-30: `1818-signal-state` was reclaimed 5.6 minutes after its PR
# merged, while the result was still being read; its last commit was 2.26 h old, so every age
# guard passed. The merge happens in the shared checkout (`gh pr merge`), so the tree's own
# last commit never moves when it matters most — only `mergedAt` sees that clock.
#
# The owner is still watching a PR this young, whatever the tree's own commit age says.
MERGE_COOLOFF_MIN = 30
# meiva:note
# ONE hour, not half: this mirrors the probe's freshness guard, so a round that runs without
# the snapshot judge (the probe is blind, or the root is not its repo) is never looser than a
# round that has it.
COMMIT_COOLOFF_MIN = 60

# meiva:note
# A probe ledger line older than this is a judge that stopped looking — the same 15 minutes the
# probe itself allows between rounds.
PROBE_MAX_AGE_S = 900.0
