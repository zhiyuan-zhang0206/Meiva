from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from base.lm.harness.codex.frames import TranscriptTap

DEFAULT_BINARY = "codex"
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
APPROVAL_POLICIES = ("never", "untrusted", "on-request")
APPROVAL_DECISIONS = ("accept", "decline")

# meiva:note
# Two hours without any frame from a child thread. A frame arrives for every message, tool item and
# status change, so a quiet child is one inside a single model call or tool call; a command a model waits
# on runs minutes, and the longest quiet stretches of a working child are well inside two hours, while a
# child whose end frame was lost would otherwise hold a runtime until the 32-runtime cache bound evicts
# it (and that bound takes held sessions last). The bound errs long because releasing a working child
# kills it, while holding a dead one costs one idle process.
# meiva:link base/lm/harness/docs/background-work.md
CHILD_SILENCE_SECONDS = 7200.0


# meiva:note
# `sandbox` defaults to danger-full-access because the bridge must run commands and
# the only alternative is a sandbox that cannot start: Ubuntu 24.04 ships
# `kernel.apparmor_restrict_unprivileged_userns=1`, so bubblewrap cannot create a
# user namespace and every read-only/workspace-write command fails with
# `bwrap: loopback: Failed RTM_NEWADDR`. The workspace boundary is the review
# boundary; the sandbox flag would only pretend to add one.
# `approval_policy` defaults to "never" so a headless turn cannot park on a prompt;
# when it is set to "untrusted"/"on-request" the server asks the bridge and
# `approval_decision` answers. `model=None` inherits the codex config default
# instead of pinning an id that the account may not serve. `transcript` is set only for an agent whose timeline is
# written from the CLI's own record (`harness_transcript_timeline`): the session then shares its app-server
# connection and its notifications with the reader of that record and resumes a thread without its history.
# meiva:link base/lm/harness/codex/docs/codex.md
@dataclass(frozen=True, slots=True)
class CodexConfig:
    cwd: Path
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox: str = "danger-full-access"
    approval_policy: str = "never"
    approval_decision: str = "accept"
    binary: str = DEFAULT_BINARY
    extra_args: tuple[str, ...] = ()
    api_key_mode: bool = False
    startup_timeout_seconds: float = 120.0
    cancel_settle_seconds: float = 5.0
    max_buffered_events: int = 256
    transcript: TranscriptTap | None = None

    def __post_init__(self) -> None:
        if self.sandbox not in SANDBOX_MODES:
            raise ValueError(f"sandbox must be one of {SANDBOX_MODES}, got {self.sandbox!r}")
        if self.approval_policy not in APPROVAL_POLICIES:
            raise ValueError(
                f"approval policy must be one of {APPROVAL_POLICIES}, got {self.approval_policy!r}"
            )
        if self.approval_decision not in APPROVAL_DECISIONS:
            raise ValueError(
                f"approval decision must be one of {APPROVAL_DECISIONS}, "
                f"got {self.approval_decision!r}"
            )
        if self.max_buffered_events < 1:
            raise ValueError("max_buffered_events must be at least 1")

    def command(self, binary: str | None = None) -> tuple[str, ...]:
        return (binary or self.binary, "app-server", "--listen", "stdio://", *self.extra_args)
