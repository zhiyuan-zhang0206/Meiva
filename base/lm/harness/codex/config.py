from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from base.host.env.harness_binaries import resolve_harness_binary

DEFAULT_BINARY = "codex"
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
APPROVAL_POLICIES = ("never", "untrusted", "on-request")
APPROVAL_DECISIONS = ("accept", "decline")


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
# instead of pinning an id that the account may not serve.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
@dataclass(frozen=True, slots=True)
class CodexConfig:
    cwd: Path
    model: str | None = None
    sandbox: str = "danger-full-access"
    approval_policy: str = "never"
    approval_decision: str = "accept"
    binary: str = DEFAULT_BINARY
    extra_args: tuple[str, ...] = ()
    api_key_mode: bool = False
    startup_timeout_seconds: float = 120.0
    turn_timeout_seconds: float = 900.0
    cancel_settle_seconds: float = 5.0
    max_pending_turns: int = 32
    max_buffered_events: int = 256
    dedupe_window: int = 256

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
        if self.max_pending_turns < 1:
            raise ValueError("max_pending_turns must be at least 1")
        if self.max_buffered_events < 1:
            raise ValueError("max_buffered_events must be at least 1")
        if self.dedupe_window < 1:
            raise ValueError("dedupe_window must be at least 1")

    # meiva:note
    # The CLI this session spawns, resolved through the directories a harness installs
    # into as well as PATH: the session environment it is handed need not carry a PATH at
    # all, and a bare name would then resolve against the OS default (#5491).
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
    def resolved_binary(
        self,
        environ: Mapping[str, str] | None,
        *,
        bin_dirs: Sequence[str] | None = None,
    ) -> str:
        return resolve_harness_binary(
            self.binary, path_env=(environ or {}).get("PATH"), bin_dirs=bin_dirs
        )

    def command(self, binary: str | None = None) -> tuple[str, ...]:
        return (binary or self.binary, "app-server", "--listen", "stdio://", *self.extra_args)
