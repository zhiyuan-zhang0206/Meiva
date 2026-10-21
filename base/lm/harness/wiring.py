from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from base.api_contracts.agent_contract import Harness
from base.host.env.harness_binaries import with_harness_path
from base.host.env.registry import child_env
from base.lm.harness.antigravity import AgySession, AntigravityConfig
from base.lm.harness.antigravity import build_handoff as antigravity_handoff
from base.lm.harness.bridge import HarnessBridge, HarnessSession, ScopedSessionRegistry
from base.lm.harness.claude import BridgeConfig, ClaudeSession
from base.lm.harness.codex import CodexConfig, CodexSession
from base.lm.harness.handoff import Handoff
from base.lm.harness.registration import HANDLE_ENV, RegistrationStore
from base.lm.harness.registry import SessionRegistry

# meiva:note
# The document a fresh session wakes on. It states who the agent is and where it
# works, and asks for one short acknowledgement: the agent's actual work arrives as
# the session's later messages, so the handoff turn must not start any. Section 3
# lists what every attached process may reach, led by `meiva.attach()`, which
# `import meiva` has already done here; what a particular role may reach is the
# role's business, not the wake's.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
HANDOFF_INSTRUCTION = (
    "You are the agent described above. Read this document once and reply with one short "
    "acknowledgement. Your work arrives as the messages that follow this document; do not "
    "start any on your own."
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
def session_environ(environ: Mapping[str, str] | None) -> dict[str, str]:
    return with_harness_path(child_env("runner") if environ is None else environ)


def open_session(
    harness: Harness,
    *,
    agent_id: int,
    cwd: Path,
    registry: SessionRegistry,
    environ: Mapping[str, str] | None = None,
    skip_permissions: bool = True,
) -> HarnessSession:
    if harness is Harness.CLAUDE_CODE:
        return ClaudeSession(
            BridgeConfig(cwd=cwd),
            environ=session_environ(environ),
            agent_id=agent_id,
            registry=registry,
        )
    if harness is Harness.CODEX:
        return CodexSession(
            CodexConfig(cwd=cwd),
            environ=environ,
            agent_id=agent_id,
            registry=registry,
        )
    if harness is Harness.ANTIGRAVITY:
        return AgySession(
            AntigravityConfig(cwd=cwd, skip_permissions=skip_permissions),
            environ=environ,
            agent_id=agent_id,
            registry=registry,
        )
    raise ValueError(f"no session adapter for harness {harness.value!r}")


# meiva:note
# The wake document is per adapter only where the adapter's world differs: Antigravity's
# CLI starts a fresh shell per `run_command` and kills a `&` child when the turn ends, and
# a harness agent that does not know that writes commands that look right and do not
# survive. Every other adapter takes the shared document unchanged.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def _handoff_for(harness: Harness, *, agent_id: int, machine: str, workspace: Path) -> Handoff:
    if harness is Harness.ANTIGRAVITY:
        return antigravity_handoff(
            agent_id=agent_id,
            instruction=HANDOFF_INSTRUCTION,
            machine=machine,
            workspace=workspace,
        )
    return Handoff(
        agent_id=agent_id,
        instruction=HANDOFF_INSTRUCTION,
        machine=machine,
        workspace=workspace,
    )


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
    environ: Mapping[str, str] | None = None,
    skip_permissions: bool = True,
) -> HarnessBridge | None:
    if harness is Harness.MEIVA:
        return None
    scoped = ScopedSessionRegistry(inner=registry, harness=harness)
    handoff = _handoff_for(harness, agent_id=agent_id, machine=machine, workspace=workspace)

    def factory(handle: str) -> HarnessSession:
        return open_session(
            harness,
            agent_id=agent_id,
            cwd=workspace,
            registry=scoped,
            environ={**session_environ(environ), HANDLE_ENV: handle},
            skip_permissions=skip_permissions,
        )

    return HarnessBridge(
        harness,
        agent_id,
        factory,
        machine=machine,
        handoff=handoff,
        registrations=registrations,
        registration_ttl_seconds=registration_ttl_seconds,
    )
