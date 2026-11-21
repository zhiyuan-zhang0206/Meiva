# meiva:note
# Workspace directory hygiene settings (task #7521): per-turn local workspace check on
# agent-host that reminds an active agent when any non-excluded directory exceeds the item
# cap, with a per-agent cooldown. Declared as configuration rather than hardcoded constants
# per the 2026-09-17 ruling that behaviour constants are configuration and every limit carries
# its reason.

from __future__ import annotations

from pydantic import Field


class WorkspaceHygieneFields:
    workspace_hygiene_enabled: bool = Field(
        title="Enable workspace directory hygiene reminders",
        default=True,
        alias="MEIVA_WORKSPACE_HYGIENE_ENABLED",
        description=(
            "Hosted agent-runner: after a normal turn finishes and transitions to idle, scan "
            "the agent's local workspace for non-excluded directories that exceed "
            "workspace_hygiene_max_items_per_dir and enqueue a cleanup reminder system_note "
            "subject to workspace_hygiene_remind_cooldown_seconds. Never scans or wakes "
            "sleeping agents; set false to disable cluster-wide."
        ),
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    workspace_hygiene_max_items_per_dir: int = Field(
        title="Maximum non-excluded items per workspace directory",
        default=20,
        ge=1,
        alias="MEIVA_WORKSPACE_HYGIENE_MAX_ITEMS_PER_DIR",
        description=(
            "Hosted agent-runner: soft cap on direct non-excluded files and subdirectories "
            "inside any scanned directory of an agent's workspace before a hygiene reminder is "
            "sent. Mirrors Meizel's 20-entry directory ceiling so flat agent workspaces do not "
            "accumulate hundreds of stale scripts and reports; excluded system paths "
            "(dot-entries, root memory/ and uploads/, meiva_instructions.md, nested git repos, "
            "and build/dependency directories) do not count toward this cap."
        ),
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    workspace_hygiene_remind_cooldown_seconds: int = Field(
        title="Workspace hygiene reminder cooldown (seconds)",
        default=86400,
        ge=1,
        alias="MEIVA_WORKSPACE_HYGIENE_REMIND_COOLDOWN_SECONDS",
        description=(
            "Hosted agent-runner: minimum seconds between workspace hygiene reminders for the "
            "same agent while its workspace remains over workspace_hygiene_max_items_per_dir. "
            "Default 86400s (24h) prevents nagging an active agent on every turn while keeping "
            "uncleaned clutter visible once per day; once every scanned directory drops back "
            "within the cap, the cooldown resets immediately."
        ),
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
