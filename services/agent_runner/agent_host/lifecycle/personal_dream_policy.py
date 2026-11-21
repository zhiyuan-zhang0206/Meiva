# meiva:note
# Pure policy, trigger evaluation, cooldown decisions, and reminder formatting for per-turn
# Personal Self-Dream checks (task #7522). Filesystem scanning, state I/O, and inbound enqueueing
# live in the sibling effect shell (`personal_dream.py`).

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from base.agents.messages.kwargs import NoteTag

__all__ = [
    "MAX_SCAN_DEPTH",
    "MAX_SCAN_DIRS",
    "PERSONAL_DREAM_SOURCE",
    "PERSONAL_DREAM_STATE_FILENAME",
    "PERSONAL_MEMORY_DIRNAME",
    "PERSONAL_MEMORY_INDEX_FILENAME",
    "PersonalDreamDecision",
    "PersonalDreamOutcome",
    "PersonalDreamState",
    "PersonalDreamTriggerReason",
    "PersonalMemoryScan",
    "build_personal_dream_inbound_payload",
    "evaluate_personal_dream",
    "format_personal_dream_reminder",
    "is_cooldown_elapsed",
    "is_excluded_memory_entry",
]

# meiva:note
# Safety bound: cap recursion depth at 4 levels below `<workspace>/memory/` so deep trees cannot
# stall turn settlement; personal memory stores are flat or have shallow topic/inherit folders.
MAX_SCAN_DEPTH = 4

# meiva:note
# Safety bound: cap total subdirectories scanned per turn at 50 so a runaway tree under `memory/`
# still completes in under ~2 ms on local SSD/APFS while BFS checks shallow entries first.
MAX_SCAN_DIRS = 50

PERSONAL_DREAM_SOURCE = "system:personal_dream"
PERSONAL_DREAM_STATE_FILENAME = ".meiva_personal_dream.json"
PERSONAL_MEMORY_DIRNAME = "memory"
PERSONAL_MEMORY_INDEX_FILENAME = "MEMORY.md"


# meiva:note
# Summary of one bounded local scan of `<workspace>/memory/`. `file_count` counts non-dot regular
# files under `memory/` excluding root `MEMORY.md` (which is measured by `index_lines`). An empty
# auto-scaffolded `MEMORY.md` (0 non-whitespace lines) is treated as absent for both `is_empty`
# and mtime bounds so fresh agents do not inherit a stale boot timestamp.
@dataclass(frozen=True)
class PersonalMemoryScan:
    index_lines: int
    file_count: int
    earliest_mtime: float | None
    latest_mtime: float | None
    dirs_scanned: int
    truncated: bool

    @property
    def is_empty(self) -> bool:
        return self.index_lines == 0 and self.file_count == 0


class PersonalDreamTriggerReason(StrEnum):
    CAPACITY = "capacity"
    ACTIVITY = "activity"
    CAPACITY_AND_ACTIVITY = "capacity_and_activity"


# meiva:note
# Pure trigger decision for one Personal Self-Dream evaluation pass.
@dataclass(frozen=True)
class PersonalDreamDecision:
    should_trigger: bool
    reason: PersonalDreamTriggerReason | None
    index_over_limit: bool
    files_over_limit: bool
    active_changes: bool


# meiva:note
# Persisted per-agent Personal Self-Dream cooldown and filesystem watermark state stored in
# `<workspace>/.meiva_personal_dream.json`. `awaiting_dream_turn` marks that a reminder was
# enqueued and its Dream turn has not yet completed; once the Dream turn settles, the host
# advances `last_reminded_at` and records the post-Dream `baseline_mtime` so the Dream turn's own
# edits to `memory/` never trigger Condition B after cooldown expires.
@dataclass(frozen=True)
class PersonalDreamState:
    last_reminded_at: float
    awaiting_dream_turn: bool = False
    baseline_mtime: float | None = None


class PersonalDreamOutcome(StrEnum):
    DISABLED = "disabled"
    EMPTY = "empty"
    COOLDOWN_ACTIVE = "cooldown_active"
    NOT_DUE = "not_due"
    SKIPPED_SELF_TRIGGER = "skipped_self_trigger"
    REMINDED = "reminded"


# meiva:note
# Dot-entries (`.DS_Store`, `.git`, temporary files) inside `<workspace>/memory/` are ignored.
def is_excluded_memory_entry(name: str) -> bool:
    return name.startswith(".")


# meiva:note
# Whether the per-agent Personal Self-Dream cooldown window has elapsed (or no reminder was ever
# sent yet).
def is_cooldown_elapsed(
    *,
    last_reminded_at: float | None,
    now_epoch: float,
    cooldown_seconds: int,
) -> bool:
    if last_reminded_at is None:
        return True
    return (now_epoch - last_reminded_at) >= cooldown_seconds


def _has_active_changes(
    scan: PersonalMemoryScan,
    *,
    state: PersonalDreamState | None,
    now_epoch: float,
    cooldown_seconds: int,
) -> bool:
    if scan.earliest_mtime is None or scan.latest_mtime is None:
        return False
    if state is None:
        return (now_epoch - scan.earliest_mtime) >= cooldown_seconds and (
            now_epoch - scan.latest_mtime
        ) <= cooldown_seconds
    watermark = state.baseline_mtime if state.baseline_mtime is not None else state.last_reminded_at
    return scan.latest_mtime > watermark


def _select_trigger_reason(
    *, capacity_hit: bool, active_changes: bool
) -> PersonalDreamTriggerReason | None:
    if capacity_hit and active_changes:
        return PersonalDreamTriggerReason.CAPACITY_AND_ACTIVITY
    if capacity_hit:
        return PersonalDreamTriggerReason.CAPACITY
    if active_changes:
        return PersonalDreamTriggerReason.ACTIVITY
    return None


# meiva:note
# Decide whether Personal Self-Dream should trigger after a normal turn:
# - Empty personal memory (`index_lines == 0` and `file_count == 0`) never triggers.
# - Active cooldown (`now - last_reminded_at < cooldown_seconds`) never triggers.
# - Condition A (capacity): `index_lines > index_max_lines` or `file_count > max_files`.
# - Condition B (active changes):
#   * First time (`state is None`): oldest personal memory has existed for at least
#     `cooldown_seconds` AND latest modification occurred within the last `cooldown_seconds`
#     (prevents firing immediately when an agent writes its very first note).
#   * Subsequent times (`state is not None`, cooldown already elapsed):
#     `latest_mtime > watermark`, where `watermark` is `baseline_mtime` (same filesystem mtime
#     clock domain captured after the Dream turn settled) or `last_reminded_at` for legacy state
#     files that predate `baseline_mtime`.
def evaluate_personal_dream(
    scan: PersonalMemoryScan,
    *,
    state: PersonalDreamState | None,
    now_epoch: float,
    cooldown_seconds: int,
    index_max_lines: int,
    max_files: int,
) -> PersonalDreamDecision:
    last_reminded_at = state.last_reminded_at if state is not None else None
    if scan.is_empty or not is_cooldown_elapsed(
        last_reminded_at=last_reminded_at,
        now_epoch=now_epoch,
        cooldown_seconds=cooldown_seconds,
    ):
        return PersonalDreamDecision(
            should_trigger=False,
            reason=None,
            index_over_limit=False,
            files_over_limit=False,
            active_changes=False,
        )

    index_over_limit = scan.index_lines > index_max_lines
    files_over_limit = scan.file_count > max_files
    capacity_hit = index_over_limit or files_over_limit
    active_changes = _has_active_changes(
        scan,
        state=state,
        now_epoch=now_epoch,
        cooldown_seconds=cooldown_seconds,
    )
    reason = _select_trigger_reason(capacity_hit=capacity_hit, active_changes=active_changes)
    return PersonalDreamDecision(
        should_trigger=reason is not None,
        reason=reason,
        index_over_limit=index_over_limit,
        files_over_limit=files_over_limit,
        active_changes=active_changes,
    )


# meiva:note
# The valid JSONB payload required for a Personal Self-Dream `system_note` inbound.
def build_personal_dream_inbound_payload() -> dict[str, object]:
    return {"note_tag": NoteTag.PERSONAL_DREAM.value}


def _describe_trigger_reasons(
    scan: PersonalMemoryScan,
    decision: PersonalDreamDecision,
    *,
    index_max_lines: int,
    max_files: int,
) -> str:
    parts: list[str] = []
    if decision.index_over_limit:
        parts.append(f"MEMORY.md exceeds {index_max_lines} lines ({scan.index_lines} lines)")
    if decision.files_over_limit:
        parts.append(f"note files exceed {max_files} ({scan.file_count} files)")
    if decision.active_changes:
        parts.append("active personal memory changes accumulated past cooldown")
    return "; ".join(parts) if parts else "scheduled personal memory review"


# meiva:note
# Render a structured 4-step Personal Self-Dream consolidation guide carrying live counts and the
# matched trigger reason(s).
def format_personal_dream_reminder(
    scan: PersonalMemoryScan,
    decision: PersonalDreamDecision,
    *,
    index_max_lines: int,
    max_files: int,
    shared_memory_path: str = "~/.meiva/vault/memory",
) -> str:
    reasons = _describe_trigger_reasons(
        scan,
        decision,
        index_max_lines=index_max_lines,
        max_files=max_files,
    )
    lines = [
        "Personal Self-Dream check: it is time to consolidate your personal memory (`memory/`).",
        f"- `memory/MEMORY.md`: {scan.index_lines} lines (soft cap: {index_max_lines})",
        f"- Note files in `memory/` (excluding `MEMORY.md` and dotfiles): "
        f"{scan.file_count} files (soft cap: {max_files})",
        f"- Trigger reason: {reasons}",
        "",
        "Please run a quick 4-step Personal Self-Dream pass:",
        f"1. Compare your personal `memory/` against the latest shared "
        f"`{shared_memory_path}/MEMORY.md` and current reality; delete outdated or retired "
        "entries and orphan note files (keep `INHERIT.md` and the `inherit/` directory — "
        "they are descendant inheritance files, not orphans).",
        "2. Merge duplicate or overlapping personal notes on the same topic.",
        f"3. Promote cross-agent reusable lessons as plain Markdown files into "
        f"`{shared_memory_path}/` (synced every 2 minutes via `land/<machine>` PR to "
        "#1609 Cluster Dream) and delete your personal duplicate to avoid double-writing.",
        f"4. Trim your personal `memory/MEMORY.md` back to <= {index_max_lines} lines "
        "(keep one pointer line per note and move detail into entry files).",
    ]
    return "\n".join(lines)
