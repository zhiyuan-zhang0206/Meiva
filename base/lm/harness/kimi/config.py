from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from base.host.env.harness_binaries import resolve_harness_binary, resolve_user_home

DEFAULT_MODEL = "kimi-k3.1-pro"
# meiva:note
# Kimi Code's ACP server (`kimi acp`) loads its own configured default model and
# thinking state on `session/new`, so a bridge that wants a specific model or
# reasoning effort sends `session/set_config_option` after session creation
# instead of mutating the user's `config.toml`. `None` leaves the CLI's own
# default in place.
# meiva:link base/lm/harness/kimi/docs/kimi.meiva.okf.md
REASONING_EFFORTS = ("off", "low", "medium", "high", "max")
PERMISSION_MODES = ("ask", "deny", "allow")


@dataclass(frozen=True, slots=True)
class KimiConfig:
    cwd: Path
    kimi_home: Path | None = None
    binary: str = "kimi"
    subcommand: tuple[str, ...] = ("acp",)
    extra_args: tuple[str, ...] = ()
    model: str | None = None
    reasoning_effort: str | None = None
    skip_permissions: bool = True
    permission_mode: str | None = None
    allowed_permission_modes: tuple[str, ...] = PERMISSION_MODES
    mcp_servers: tuple[Mapping[str, object], ...] = ()
    turn_timeout_seconds: float = 900.0
    startup_timeout_seconds: float = 120.0
    cancel_settle_seconds: float = 10.0
    shutdown_timeout_seconds: float = 10.0
    max_pending_turns: int = 32
    dedupe_window: int = 256
    max_buffered_events: int = 256
    env_overrides: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        effective_mode = (
            self.permission_mode
            if self.permission_mode is not None
            else ("allow" if self.skip_permissions else "ask")
        )
        if effective_mode not in self.allowed_permission_modes:
            raise ValueError(
                f"unknown permission_mode {effective_mode!r}; "
                f"known modes: {', '.join(self.allowed_permission_modes)}"
            )
        object.__setattr__(self, "permission_mode", effective_mode)
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"unknown reasoning_effort {self.reasoning_effort!r}; "
                f"known efforts: {', '.join(REASONING_EFFORTS)}"
            )

    @property
    def workspace(self) -> str:
        return str(self.cwd)

    def resolved_binary(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        bin_dirs: Sequence[str] | None = None,
    ) -> str:
        return resolve_harness_binary(
            self.binary,
            path_env=(environ or {}).get("PATH"),
            bin_dirs=bin_dirs,
        )

    def command(self, binary: str | None = None) -> list[str]:
        return [
            binary or self.binary,
            *self.subcommand,
            *self.extra_args,
        ]

    def resolved_kimi_home(self, environ: Mapping[str, str] | None = None) -> Path:
        if self.kimi_home is not None:
            return self.kimi_home
        env = environ or {}
        configured = env.get("KIMI_CODE_HOME")
        if configured:
            return Path(configured)
        home = env.get("HOME")
        return resolve_user_home(Path(home) if home else None) / ".kimi-code"

    def with_cwd(self, cwd: Path) -> KimiConfig:
        return KimiConfig(
            cwd=cwd,
            kimi_home=self.kimi_home,
            binary=self.binary,
            subcommand=self.subcommand,
            extra_args=self.extra_args,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            skip_permissions=self.skip_permissions,
            permission_mode=self.permission_mode,
            allowed_permission_modes=self.allowed_permission_modes,
            mcp_servers=self.mcp_servers,
            turn_timeout_seconds=self.turn_timeout_seconds,
            startup_timeout_seconds=self.startup_timeout_seconds,
            cancel_settle_seconds=self.cancel_settle_seconds,
            shutdown_timeout_seconds=self.shutdown_timeout_seconds,
            max_pending_turns=self.max_pending_turns,
            dedupe_window=self.dedupe_window,
            max_buffered_events=self.max_buffered_events,
            env_overrides=self.env_overrides,
        )
