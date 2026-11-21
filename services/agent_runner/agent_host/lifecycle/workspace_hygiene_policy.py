# meiva:note
# Pure policy, exclusion rules, cooldown decisions, and reminder formatting for per-turn
# workspace directory hygiene checks (task #7521). Filesystem scanning, state I/O, and
# inbound enqueueing live in the sibling effect shell (`workspace_hygiene.py`).

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from base.agents.messages.kwargs import NoteTag

__all__ = [
    "MAX_REPORTED_DIRS",
    "MAX_SCAN_DEPTH",
    "MAX_SCAN_DIRS",
    "NESTED_GIT_MARKER",
    "ROOT_EXCLUDED_NAMES",
    "SKIPPED_BUILD_OR_DEP_DIRS",
    "WORKSPACE_HYGIENE_SOURCE",
    "WORKSPACE_HYGIENE_STATE_FILENAME",
    "DirectoryOverflow",
    "HygieneCheckOutcome",
    "HygieneScanResult",
    "build_hygiene_inbound_payload",
    "format_workspace_hygiene_reminder",
    "is_excluded_entry",
    "should_remind",
]

# meiva:note
# Safety bound: cap recursion depth at 4 levels below workspace root so deep generated trees
# cannot stall turn settlement; agent-organized workspaces rarely nest active files deeper.
MAX_SCAN_DEPTH = 4

# meiva:note
# Safety bound: cap total directories scanned per turn at 200 so a runaway tree still
# completes in under ~2 ms on local SSD/APFS while BFS ensures shallow dirs are checked first.
MAX_SCAN_DIRS = 200

# meiva:note
# Display bound: show at most the 8 worst directories in one reminder so the note stays brief
# and actionable instead of flooding the agent's context window.
MAX_REPORTED_DIRS = 8

WORKSPACE_HYGIENE_SOURCE = "system:workspace_hygiene"
WORKSPACE_HYGIENE_STATE_FILENAME = ".meiva_workspace_hygiene.json"
NESTED_GIT_MARKER = ".git"

# meiva:note
# System-managed paths at workspace root that belong to framework/platform features and must
# never be counted or rearranged by workspace hygiene reminders.
ROOT_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        "memory",
        "uploads",
        "meiva_instructions.md",
    }
)

# meiva:note
# Common build-artifact and dependency directories whose entry counts are machine-managed.
# Dot-prefixed equivalents (e.g. `.venv`, `.pytest_cache`, `.ruff_cache`, `.git`) are already
# excluded by the leading-dot rule in `is_excluded_entry`.
SKIPPED_BUILD_OR_DEP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        "venv",
    }
)


# meiva:note
# One workspace directory whose non-excluded direct entry count exceeded the cap.
@dataclass(frozen=True)
class DirectoryOverflow:
    relative_path: str
    item_count: int


# meiva:note
# Outcome of one bounded local workspace directory scan.
@dataclass(frozen=True)
class HygieneScanResult:
    overflows: tuple[DirectoryOverflow, ...]
    dirs_scanned: int
    truncated: bool


class HygieneCheckOutcome(StrEnum):
    DISABLED = "disabled"
    CLEAN = "clean"
    CLEAN_RESET = "clean_reset"
    COOLDOWN_ACTIVE = "cooldown_active"
    SKIPPED_SELF_TRIGGER = "skipped_self_trigger"
    REMINDED = "reminded"


# meiva:note
# Whether a direct entry inside a scanned workspace directory is excluded from hygiene.
def is_excluded_entry(name: str, *, depth: int, is_dir: bool) -> bool:
    if name.startswith("."):
        return True
    if depth == 0 and name in ROOT_EXCLUDED_NAMES:
        return True
    return is_dir and name in SKIPPED_BUILD_OR_DEP_DIRS


# meiva:note
# Whether enough time has elapsed since the previous reminder to send another.
def should_remind(
    *,
    last_reminded_at: float | None,
    now_epoch: float,
    cooldown_seconds: int,
) -> bool:
    if last_reminded_at is None:
        return True
    return (now_epoch - last_reminded_at) >= cooldown_seconds


# meiva:note
# The valid JSONB payload required for a workspace hygiene `system_note` inbound.
def build_hygiene_inbound_payload() -> dict[str, object]:
    return {"note_tag": NoteTag.WORKSPACE_HYGIENE.value}


# meiva:note
# Render a concise cleanup reminder listing the worst overflowing directories without imposing
# rigid subdirectory naming conventions.
def format_workspace_hygiene_reminder(
    overflows: Sequence[DirectoryOverflow],
    *,
    max_items_per_dir: int,
    max_reported_dirs: int = MAX_REPORTED_DIRS,
) -> str:
    shown = overflows[:max_reported_dirs]
    lines = [
        f"Workspace hygiene check: the following directories in your workspace exceed the "
        f"{max_items_per_dir}-item limit:"
    ]
    for entry in shown:
        label = ". (workspace root)" if entry.relative_path == "." else entry.relative_path
        lines.append(f"- {label}: {entry.item_count} items")
    remaining = len(overflows) - len(shown)
    if remaining > 0:
        lines.append(f"- ... and {remaining} more directories over the limit.")
    lines.append("")
    lines.append(
        f"Please tidy them back to <= {max_items_per_dir} items per directory by deleting "
        "stale temporary files or grouping older files into subdirectories (use whatever "
        "organization fits your work). Do not break any paths still referenced in `to_user.md`."
    )
    return "\n".join(lines)
