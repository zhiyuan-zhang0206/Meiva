from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from base.api_contracts.agent_contract import Harness
from base.host.env.harness_binaries import resolve_harness_binary, with_harness_path
from base.host.env.registry import process_environment
from base.lm.harness.antigravity import AgySession, AntigravityConfig
from base.lm.harness.antigravity import build_handoff as antigravity_handoff
from base.lm.harness.base_session import SessionBinding, SessionClock
from base.lm.harness.bridge import HarnessBridge, HarnessSession, ScopedSessionRegistry
from base.lm.harness.claude import BridgeConfig, ClaudeSession
from base.lm.harness.codex import CodexConfig
from base.lm.harness.codex.session import CodexSession
from base.lm.harness.events import UnsolicitedSink
from base.lm.harness.handoff import ClusterFact, Handoff, HarnessContribution
from base.lm.harness.kimi import KimiConfig, KimiSession
from base.lm.harness.registration import HANDLE_ENV, RegistrationStore, SecureRandom
from base.lm.harness.registry import SessionRegistry

# meiva:note
# The document a fresh session wakes on. It states who the agent is and where it
# works, and sits at `state.messages[0]` so the first inbound turn co-delivers the
# handoff and the user's messages in one submission without overriding the CLI's
# native system prompt. Section 3 lists what every attached process may reach, led
# by `meiva.attach()`, which `import meiva` has already done here; what a particular
# role may reach is the role's business, not the wake's.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
HANDOFF_INSTRUCTION = (
    "You are the agent described above. Follow the rules in this document for every turn, "
    "and act on the messages that follow it."
)


# meiva:note
# One adapter session per harness. A harness without an entry is a value the release
# cannot run, and it fails here rather than starting a session with another
# provider's flags.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
# meiva:note
# The environment a harness session runs with. The daemon child environment this starts
# from is a positive allowlist that carries no PATH, and a session delivery point is where
# it is rebuilt — the CLI and everything it runs resolve through this (#5491).
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def session_environ(environ: Mapping[str, str]) -> dict[str, str]:
    base = dict(environ)
    venv_bin = str(Path(sys.executable).parent)
    existing = base.get("PATH")
    base["PATH"] = f"{venv_bin}{os.pathsep}{existing}" if existing else venv_bin
    return with_harness_path(base)


def open_session(
    harness: Harness,
    *,
    agent_id: int,
    cwd: Path,
    registry: SessionRegistry,
    environ: Mapping[str, str],
    clock: SessionClock,
    skip_permissions: bool = True,
    unsolicited: UnsolicitedSink | None = None,
) -> HarnessSession:
    if harness is Harness.CLAUDE_CODE:
        claude_env = session_environ(environ)
        return ClaudeSession(
            BridgeConfig(
                cwd=cwd,
                cli_path=resolve_harness_binary("claude", path_env=claude_env.get("PATH")),
            ),
            environ={**dict.fromkeys(process_environment(), ""), **claude_env},
            binding=SessionBinding(agent_id, registry, unsolicited),
            clock=clock,
        )
    if harness is Harness.CODEX:
        return CodexSession(
            CodexConfig(cwd=cwd),
            environ=environ,
            binding=SessionBinding(agent_id, registry),
            clock=clock,
        )
    if harness is Harness.ANTIGRAVITY:
        return AgySession(
            AntigravityConfig(cwd=cwd, skip_permissions=skip_permissions),
            environ=environ,
            binding=SessionBinding(agent_id, registry),
            clock=clock,
        )
    if harness is Harness.KIMI_CODE:
        return KimiSession(
            KimiConfig(cwd=cwd, skip_permissions=skip_permissions),
            environ=environ,
            binding=SessionBinding(agent_id, registry),
            clock=clock,
        )
    raise ValueError(f"no session adapter for harness {harness.value!r}")


# meiva:note
# The wake document is per adapter only where the adapter's world differs: Antigravity's
# CLI starts a fresh shell per `run_command` and kills a `&` child when the turn ends, and
# a harness agent that does not know that writes commands that look right and do not
# survive. Every other adapter takes the shared document unchanged.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def handoff_for(
    harness: Harness,
    *,
    agent_id: int,
    machine: str,
    workspace: Path,
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
            label=label,
            cluster=cluster,
        )
    else:
        base = Handoff(
            agent_id=agent_id,
            instruction=instruction,
            machine=machine,
            workspace=workspace,
            label=label,
            cluster=tuple(cluster),
        )
    return base.with_contributions(contributions, harness=harness)


_handoff_for = handoff_for


# meiva:note
# What the host root read once for the bridges it builds: the clock their turns and
# registrations read, the controller pid a registration names, and the generator the session
# handle (a bearer credential) is drawn from, which the root makes a `random.SystemRandom`.
@dataclass(frozen=True)
class BridgeRootValues:
    clock: SessionClock
    controller_pid: int
    handle_rng: SecureRandom


# meiva:note
# The bridge one runtime carries: none for the built-in loop, one per external
# session otherwise. The handoff is built here from what the host knows — the agent
# id, the machine it runs on and its workspace — so the waking path has one place to
# change and the graph sees only `run_turn` and `interrupt`.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def bridge_for(
    harness: Harness,
    *,
    agent_id: int,
    machine: str,
    workspace: Path,
    registry: SessionRegistry,
    registrations: RegistrationStore | None = None,
    registration_ttl_seconds: float,
    environ: Mapping[str, str],
    root: BridgeRootValues,
    skip_permissions: bool = True,
    contributions: Iterable[HarnessContribution] = (),
    unsolicited: UnsolicitedSink | None = None,
) -> HarnessBridge | None:
    if harness is Harness.MEIVA:
        return None
    scoped = ScopedSessionRegistry(inner=registry, harness=harness)
    handoff = _handoff_for(
        harness,
        agent_id=agent_id,
        machine=machine,
        workspace=workspace,
        contributions=contributions,
    )

    def factory(handle: str) -> HarnessSession:
        return open_session(
            harness,
            agent_id=agent_id,
            cwd=workspace,
            registry=scoped,
            environ={**session_environ(environ), HANDLE_ENV: handle},
            clock=root.clock,
            skip_permissions=skip_permissions,
            unsolicited=unsolicited,
        )

    return HarnessBridge(
        harness,
        agent_id,
        factory,
        machine=machine,
        handoff=handoff,
        registrations=registrations,
        registration_ttl_seconds=registration_ttl_seconds,
        clock=root.clock,
        handle_rng=root.handle_rng,
        controller_pid=root.controller_pid,
    )
