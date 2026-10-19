from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# meiva:note
# The full model id from `agy models`, never an alias or a display name: the CLI
# resolves an unknown id to its own default, so a short name would silently move
# the bridge onto another model after an upgrade.
# meiva:link base/lm/harness/antigravity/docs/antigravity.meiva.okf.md
DEFAULT_MODEL = "gemini-4-argon-high"


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
    turn_timeout_seconds: float = 900.0
    cancel_settle_seconds: float = 5.0
    max_pending_turns: int = 32
    max_buffered_events: int = 256
    dedupe_window: int = 256

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
