from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from base.lm.harness.antigravity.schedules import UNBOUNDED_SCHEDULE_SECONDS

# meiva:note
# The full model id from `agy models`, never an alias or a display name: the CLI
# resolves an unknown id to its own default, so a short name would silently move
# the bridge onto another model after an upgrade.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
DEFAULT_MODEL = "gemini-4-argon-high"


def resolve_antigravity_cli_model(model: str, reasoning_effort: str | None = None) -> str:
    if not reasoning_effort:
        return model
    suffix = f"-{reasoning_effort}"
    return model if model.endswith(suffix) else f"{model}{suffix}"


# meiva:note
# Everything the adapter decides about one `agy` process. Bounds carry their
# reason in the component doc; a limit without one is a defect.
@dataclass(frozen=True, slots=True)
class AntigravityConfig:
    cwd: Path
    model: str = DEFAULT_MODEL
    # meiva:note
    # An argv prefix rather than a program path, so a test can point the adapter
    # at a fake CLI without a shell.
    executable: tuple[str, ...] = ("agy",)
    # meiva:note
    # Headless `agy` cannot ask a human: a command outside the configured policy
    # is denied instead of prompted. Opting in here trades that denial for
    # auto-approval.
    skip_permissions: bool = False
    extra_args: tuple[str, ...] = ()
    cancel_settle_seconds: float = 5.0
    # meiva:note
    # How long an image waits for its turn to be running before it is given up on (see the session).
    image_follow_seconds: float = 30.0
    max_buffered_events: int = 256
    # meiva:note
    # Upper bound for extending the foreground stall-watchdog silence floor for one explicit timed
    # `schedule(DurationSeconds=...)` call; defaults to `UNBOUNDED_SCHEDULE_SECONDS` (24h).
    max_schedule_timer_seconds: float = UNBOUNDED_SCHEDULE_SECONDS

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.max_schedule_timer_seconds)
            or self.max_schedule_timer_seconds <= 0
        ):
            raise ValueError(
                "max_schedule_timer_seconds must be a finite positive number, "
                f"got {self.max_schedule_timer_seconds!r}"
            )

    def command(self, *, resume: str | None) -> tuple[str, ...]:
        argv = [
            *self.executable,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            self.model,
        ]
        if resume is not None:
            argv.extend(("--conversation", resume))
        if self.skip_permissions:
            argv.append("--dangerously-skip-permissions")
        argv.extend(self.extra_args)
        return tuple(argv)
