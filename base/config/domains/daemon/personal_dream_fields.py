# meiva:note
# Personal Self-Dream trigger settings (task #7522): per-turn local `<workspace>/memory/` check on
# agent-host that reminds an active agent to consolidate its personal memory when capacity limits
# are exceeded or active personal-memory changes accumulate past cooldown. Declared as configuration
# rather than hardcoded constants per the 2026-09-17 ruling that behaviour constants are
# configuration and every limit carries its reason.

from __future__ import annotations

from pydantic import Field


class PersonalDreamFields:
    personal_dream_enabled: bool = Field(
        title="Enable personal Self-Dream reminders",
        default=True,
        alias="MEIVA_PERSONAL_DREAM_ENABLED",
        description=(
            "Hosted agent-runner: after a normal turn finishes and transitions to idle, inspect "
            "the agent's local `<workspace>/memory/` directory and enqueue a Personal Self-Dream "
            "consolidation reminder system_note when capacity or active-change criteria match "
            "past personal_dream_cooldown_seconds. Never scans or wakes sleeping agents; set "
            "false to disable cluster-wide."
        ),
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    personal_dream_cooldown_seconds: int = Field(
        title="Personal Self-Dream reminder cooldown (seconds)",
        default=604800,
        ge=1,
        alias="MEIVA_PERSONAL_DREAM_COOLDOWN_SECONDS",
        description=(
            "Hosted agent-runner: minimum seconds between Personal Self-Dream reminders for the "
            "same agent, and the activity window used on first-time checks before any prior "
            "reminder exists. Default 604800s (7 days) matches a weekly personal memory "
            "consolidation cadence so active agents prune stale notes and promote shared lessons "
            "without being interrupted on every turn."
        ),
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    personal_dream_index_max_lines: int = Field(
        title="Personal memory index soft cap for Self-Dream (lines)",
        default=150,
        ge=1,
        alias="MEIVA_PERSONAL_DREAM_INDEX_MAX_LINES",
        description=(
            "Hosted agent-runner: soft line threshold on `<workspace>/memory/MEMORY.md` above "
            "which a Personal Self-Dream reminder triggers (subject to cooldown). Default 150 "
            "lines intervenes earlier than the 200-line cold-start injection warning so the "
            "always-injected personal index stays compact and high-signal."
        ),
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    personal_dream_max_files: int = Field(
        title="Personal memory note file soft cap for Self-Dream",
        default=40,
        ge=1,
        alias="MEIVA_PERSONAL_DREAM_MAX_FILES",
        description=(
            "Hosted agent-runner: soft cap on non-dot note files inside `<workspace>/memory/` "
            "(excluding `MEMORY.md`) above which a Personal Self-Dream reminder triggers (subject "
            "to cooldown). Default 40 keeps an agent's personal memory store small enough to "
            "review in one consolidation pass without accumulating unindexed orphan notes."
        ),
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
