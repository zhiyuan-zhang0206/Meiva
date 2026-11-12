from __future__ import annotations

from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path

from base.lm.harness.claude.auth import AuthConfig

DEFAULT_MODEL = "claude-sonnet-5-5"


# meiva:note
# Every bound here is a knob and its reason lives in the component doc — a limit
# without a stated reason is a defect, not a default. `DEFAULT_MODEL` is the full
# id from the model table of the CLI bundled with the SDK, never an alias, so an
# SDK upgrade cannot silently move the bridge onto another model.
# meiva:link base/lm/harness/claude/docs/claude.md
@dataclass(frozen=True, slots=True)
class BridgeConfig:
    cwd: Path
    model: str = DEFAULT_MODEL
    reasoning_effort: str | None = None
    cli_path: str | None = None
    tools: tuple[str, ...] | None = None
    permission_mode: str = "bypassPermissions"
    auth: AuthConfig = field(default_factory=AuthConfig)
    cancel_settle_seconds: float = 5.0
    max_buffered_events: int = 256
    # meiva:note
    # The zone the CLI process interprets a cron expression in: its own `TZ` when the launcher gives it
    # one, else the host process's local zone, which the CLI inherits. The launcher knows it and hands
    # it down as a value; None means unknown, and a one-shot cron is then not dated from its expression.
    # meiva:link base/lm/harness/claude/docs/background-tasks.md
    cron_timezone: tzinfo | None = None
    # meiva:note
    # The `session_store` the SDK mirrors the transcript to, which only wakes the timeline writer
    # (`transcript/wake.py`); None leaves the SDK's own default and no mirror.
    # meiva:link base/lm/harness/docs/transcript-projection.md
    session_store: object | None = None
