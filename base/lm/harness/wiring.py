from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from base.api_contracts.agent_contract import Harness
from base.host.env.harness_binaries import (
    meiva_python_env,
    noninteractive_bash_path,
    resolve_harness_binary,
    with_harness_path,
)
from base.host.env.registry import process_environment
from base.lm.harness.antigravity import (
    AgySession,
    AgyTransport,
    AntigravityConfig,
    resolve_antigravity_cli_model,
)
from base.lm.harness.antigravity import build_handoff as antigravity_handoff
from base.lm.harness.base_session import SessionBinding, SessionClock
from base.lm.harness.bridge import HarnessBridge, HarnessSession
from base.lm.harness.claude import BridgeConfig
from base.lm.harness.claude.session import ClaudeSession
from base.lm.harness.codex import CodexConfig
from base.lm.harness.codex.session import CodexSession
from base.lm.harness.events import UnsolicitedSink
from base.lm.harness.handoff import ClusterFact, Handoff, HarnessContribution, refreshes_for
from base.lm.harness.registration import HANDLE_ENV, CallerRegistry, RegistrationStore, SecureRandom
from base.lm.harness.registry import SessionRegistry
from base.log import logger

# meiva:note
# The document a fresh session wakes on. It states who the agent is and where it
# works, and sits at `state.messages[0]` so the first inbound turn co-delivers the
# handoff and the user's messages in one submission without overriding the CLI's
# native system prompt. Section 3 lists what every attached process may reach, led
# by `meiva`, whose identity binds on first use here; what a particular
# role may reach is the role's business, not the wake's.
# meiva:link agent/graph/docs/harness-turn.md
HANDOFF_INSTRUCTION = (
    "You are the agent described above. Follow the rules in this document for every turn, "
    "and act on the messages that follow it."
)


# meiva:note
# One adapter session per harness. A harness without an entry is a value the release
# cannot run, and it fails here rather than starting a session with another
# provider's flags.
# meiva:link agent/graph/docs/harness-turn.md
# meiva:note
# The environment a harness session runs with. The daemon child environment this starts
# from is a positive allowlist that carries no PATH, and a session delivery point is where
# it is rebuilt — the CLI and everything it runs resolve through this (#5491).
# meiva:link base/lm/harness/docs/harness.md
def session_environ(environ: Mapping[str, str]) -> dict[str, str]:
    base = dict(environ)
    venv_bin = str(Path(sys.executable).parent)
    existing = base.get("PATH")
    if existing:
        entries = existing.split(os.pathsep)
        if entries[0] != venv_bin:
            base["PATH"] = f"{venv_bin}{os.pathsep}{existing}"
    else:
        base["PATH"] = venv_bin
    base.update(meiva_python_env(sys.executable))
    base["SHELL"] = noninteractive_bash_path()
    return with_harness_path(base)


# meiva:note
# The zone a harness CLI child runs in, which is how it reads a cron expression: the `TZ` the launcher
# passes (or the host's own, which the child inherits), else the host process's local zone.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
def cli_timezone(environ: Mapping[str, str]) -> tzinfo | None:
    name = environ.get("TZ")
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return None
    return datetime.now().astimezone().tzinfo


def _with_collector_bypass(environ: Mapping[str, str]) -> dict[str, str]:
    result = dict(environ)
    populated = tuple(key for key in ("NO_PROXY", "no_proxy") if result.get(key))
    for key in populated or ("no_proxy" if "no_proxy" in environ else "NO_PROXY",):
        value = result.get(key, "")
        if value != "*":
            result[key] = ",".join(filter(None, (value, "127.0.0.1")))
    return result


def open_session(
    harness: Harness,
    *,
    agent_id: int,
    cwd: Path,
    registry: SessionRegistry,
    environ: Mapping[str, str],
    clock: SessionClock,
    model: str | None = None,
    reasoning_effort: str | None = None,
    skip_permissions: bool = True,
    unsolicited: UnsolicitedSink | None = None,
    fork_source_agent_id: int | None = None,
    otlp_endpoint: str | None = None,
    trace_enabled: bool = False,
) -> HarnessSession:
    if otlp_endpoint is not None:
        environ = _with_collector_bypass(environ)
    binding = SessionBinding(agent_id, registry, unsolicited, fork_source_agent_id)
    if harness is Harness.CLAUDE_CODE:
        claude_env = session_environ(environ)
        claude_config = BridgeConfig(
            cwd=cwd,
            agent_id=agent_id,
            cron_timezone=cli_timezone(environ),
            reasoning_effort=reasoning_effort,
            otlp_endpoint=otlp_endpoint,
            trace_enabled=trace_enabled,
            cli_path=resolve_harness_binary("claude", path_env=claude_env.get("PATH")),
        )
        if model is not None:
            claude_config = replace(claude_config, model=model)
        return ClaudeSession(
            claude_config,
            environ={**dict.fromkeys(process_environment(), ""), **claude_env},
            binding=binding,
            clock=clock,
        )
    if harness is Harness.CODEX:
        return CodexSession(
            CodexConfig(
                cwd=cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                otlp_endpoint=otlp_endpoint,
                trace_enabled=trace_enabled,
            ),
            environ=environ,
            binding=binding,
            clock=clock,
        )
    if harness is Harness.ANTIGRAVITY:
        antigravity_config = AntigravityConfig(cwd=cwd, skip_permissions=skip_permissions)
        if model is not None:
            antigravity_config = replace(
                antigravity_config,
                model=resolve_antigravity_cli_model(model, reasoning_effort),
            )
        owner_uid = os.getuid()
        return AgySession(
            antigravity_config,
            environ=environ,
            binding=binding,
            clock=clock,
            transport_factory=lambda: AgyTransport.create(
                antigravity_config,
                environ=environ,
                owner_uid=owner_uid,
                generation_id=uuid4().hex,
                clock=clock,
            ),
        )
    raise ValueError(f"no session adapter for harness {harness.value!r}")


# meiva:note
# Codex Computer Use dropped the Chinese portion of a mixed-script string in Chrome.
# The wake document gives fresh Codex sessions the verified plain-text input path.
# meiva:link base/lm/harness/codex/docs/codex.md
_CODEX_COMPUTER_USE = HarnessContribution(
    harnesses=frozenset({Harness.CODEX}),
    overlaps=(
        "When inserting non-ASCII text with Codex Computer Use on macOS, use "
        "`paste(text, {format: 'text'})` and read back the field to verify the exact text. "
        "Native `typeText` can drop characters from mixed-script input.",
    ),
)


def handoff_for(
    harness: Harness,
    *,
    agent_id: int,
    machine: str,
    workspace: Path,
    home: Path,
    model: str | None = None,
    reasoning_effort: str | None = None,
    instruction: str = HANDOFF_INSTRUCTION,
    label: str | None = None,
    cluster: Iterable[ClusterFact] = (),
    contributions: Iterable[HarnessContribution] = (),
) -> Handoff:
    if harness is Harness.ANTIGRAVITY:
        base = antigravity_handoff(
            agent_id=agent_id,
            instruction=instruction,
            machine=machine,
            workspace=workspace,
            home=home,
            label=label,
            cluster=cluster,
        )
    else:
        base = Handoff(
            agent_id=agent_id,
            instruction=instruction,
            machine=machine,
            workspace=workspace,
            home=home,
            label=label,
            cluster=tuple(cluster),
        )
    base = replace(base, harness=harness, model=model, reasoning_effort=reasoning_effort)
    return base.with_contributions((_CODEX_COMPUTER_USE, *contributions), harness=harness)


_handoff_for = handoff_for


@dataclass(frozen=True)
class HarnessOtlp:
    endpoint: str
    traces: bool

    async def available(self) -> bool:
        try:
            async with (
                asyncio.timeout(0.5),
                httpx.AsyncClient(trust_env=False) as client,
                client.stream(
                    "POST",
                    f"{self.endpoint}/v1/traces",
                    content=b"",
                    headers={"Content-Type": "application/x-protobuf"},
                ) as response,
            ):
                if (
                    not response.is_success
                    or response.headers.get("X-Meiva-Harness-Redaction") != "1"
                ):
                    return False
                size = 0
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > 16:
                        return False
        except (TimeoutError, httpx.HTTPError):
            return False
        return True


# meiva:note
# What the host root read once for the bridges it builds: the clock their turns and
# registrations read, the controller pid a registration names, and the generator the session
# handle (a bearer credential) is drawn from, which the root makes a `random.SystemRandom`, and what
# to tell the host (with the agent id) when a CLI begins a turn of its own while nothing is listening.
@dataclass(frozen=True)
class BridgeRootValues:
    clock: SessionClock
    controller_pid: int
    handle_rng: SecureRandom
    own_turn: Callable[[int], None]
    timestamp_prefix: Callable[[], str]
    caller_registry: CallerRegistry | None = None
    telemetry: HarnessOtlp | None = None


# meiva:note
# The bridge one runtime carries: none for the built-in loop, one per external
# session otherwise. The handoff is built here from what the host knows — the agent
# id, the machine it runs on and its workspace — so the waking path has one place to
# change and the graph sees only `run_turn` and `interrupt`.
# meiva:link agent/graph/docs/harness-turn.md
def bridge_for(
    harness: Harness,
    *,
    agent_id: int,
    machine: str,
    workspace: Path,
    home: Path,
    registry: SessionRegistry,
    registrations: RegistrationStore | None = None,
    registration_ttl_seconds: float,
    environ: Mapping[str, str],
    root: BridgeRootValues,
    model: str | None = None,
    reasoning_effort: str | None = None,
    skip_permissions: bool = True,
    contributions: Iterable[HarnessContribution] = (),
    unsolicited: UnsolicitedSink | None = None,
    fork_source_agent_id: int | None = None,
) -> HarnessBridge | None:
    if harness is Harness.MEIVA:
        return None
    contributions_tuple = tuple(contributions)
    handoff = _handoff_for(
        harness,
        agent_id=agent_id,
        machine=machine,
        workspace=workspace,
        home=home,
        contributions=contributions_tuple,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    refreshes = refreshes_for(contributions_tuple, harness=harness)

    async def factory(handle: str) -> HarnessSession:
        telemetry = root.telemetry if harness in (Harness.CLAUDE_CODE, Harness.CODEX) else None
        if telemetry is not None and not await telemetry.available():
            logger.warning(
                "Native harness OTLP disabled: the local redaction receiver is unavailable"
            )
            telemetry = None
        return open_session(
            harness,
            agent_id=agent_id,
            cwd=workspace,
            registry=registry,
            environ={**session_environ(environ), HANDLE_ENV: handle},
            clock=root.clock,
            model=model,
            reasoning_effort=reasoning_effort,
            skip_permissions=skip_permissions,
            unsolicited=unsolicited,
            fork_source_agent_id=fork_source_agent_id,
            otlp_endpoint=telemetry.endpoint if telemetry is not None else None,
            trace_enabled=telemetry is not None and telemetry.traces,
        )

    return HarnessBridge(
        harness,
        agent_id,
        factory,
        machine=machine,
        handoff=handoff,
        home=home,
        workspace=workspace,
        refreshes=refreshes,
        registrations=registrations,
        registration_ttl_seconds=registration_ttl_seconds,
        caller_registry=root.caller_registry,
        clock=root.clock,
        handle_rng=root.handle_rng,
        controller_pid=root.controller_pid,
        own_turn=lambda: root.own_turn(agent_id),
        timestamp_prefix=root.timestamp_prefix,
    )
