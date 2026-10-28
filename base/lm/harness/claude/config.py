from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from base.lm.harness.claude.auth import AuthConfig

DEFAULT_MODEL = "claude-sonnet-5-5"


# meiva:note
# Every bound here is a knob and its reason lives in the component doc — a limit
# without a stated reason is a defect, not a default. `DEFAULT_MODEL` is the full
# id from the model table of the CLI bundled with the SDK, never an alias, so an
# SDK upgrade cannot silently move the bridge onto another model.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
@dataclass(frozen=True, slots=True)
class BridgeConfig:
    cwd: Path
    model: str = DEFAULT_MODEL
    reasoning_effort: str | None = None
    cli_path: str | None = None
    tools: tuple[str, ...] | None = None
    permission_mode: str = "bypassPermissions"
    max_turns: int | None = None
    auth: AuthConfig = field(default_factory=AuthConfig)
    turn_timeout_seconds: float = 900.0
    cancel_settle_seconds: float = 5.0
    max_pending_turns: int = 32
    max_buffered_events: int = 256
    dedupe_window: int = 256
    injected_turn_timeout_seconds: float = 30.0
