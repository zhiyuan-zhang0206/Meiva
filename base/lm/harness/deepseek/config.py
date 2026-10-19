from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PROFILE = "acp"
# meiva:note
# The ACP profile's shipped session template pins its own provider and model, so a
# bridge that wants another one sends `session/set_config_option` after
# `session/new` instead of editing the profile. `reasoning_effort` is the other
# advertised selector; None leaves the model's own default in place.
# meiva:link base/lm/harness/deepseek/docs/deepseek.meiva.okf.md
REASONING_EFFORTS = ("off", "low", "high", "max")


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    cwd: Path
    dsh_home: Path
    binary: str = "dsh"
    profile: str = DEFAULT_PROFILE
    model: str | None = DEFAULT_MODEL
    reasoning_effort: str | None = None
    mcp_servers: tuple[Mapping[str, object], ...] = ()
    turn_timeout_seconds: float = 900.0
    startup_timeout_seconds: float = 120.0
    cancel_settle_seconds: float = 10.0
    shutdown_timeout_seconds: float = 10.0
    max_pending_turns: int = 32
    dedupe_window: int = 256
    max_buffered_events: int = 256
    # meiva:note
    # Every bound here is a knob and its reason lives in the component doc.
    # `turn_timeout_seconds` and `cancel_settle_seconds` are separate because a
    # turn that overruns is interrupted (invariant 5) while the settle window only
    # covers how long the runtime may take to acknowledge that interruption.
    permission_mode: str = "ask"
    allowed_permission_modes: tuple[str, ...] = ("ask", "deny", "allow")
    env_overrides: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        if self.permission_mode not in self.allowed_permission_modes:
            raise ValueError(
                f"unknown permission_mode {self.permission_mode!r}; "
                f"known modes: {', '.join(self.allowed_permission_modes)}"
            )
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"unknown reasoning_effort {self.reasoning_effort!r}; "
                f"known efforts: {', '.join(REASONING_EFFORTS)}"
            )

    @property
    def workspace(self) -> str:
        return str(self.cwd)

    def with_cwd(self, cwd: Path) -> BridgeConfig:
        return BridgeConfig(
            cwd=cwd,
            dsh_home=self.dsh_home,
            binary=self.binary,
            profile=self.profile,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            mcp_servers=self.mcp_servers,
            turn_timeout_seconds=self.turn_timeout_seconds,
            startup_timeout_seconds=self.startup_timeout_seconds,
            cancel_settle_seconds=self.cancel_settle_seconds,
            shutdown_timeout_seconds=self.shutdown_timeout_seconds,
            max_pending_turns=self.max_pending_turns,
            dedupe_window=self.dedupe_window,
            max_buffered_events=self.max_buffered_events,
            permission_mode=self.permission_mode,
            allowed_permission_modes=self.allowed_permission_modes,
            env_overrides=self.env_overrides,
        )
