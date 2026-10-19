from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from base.api_contracts.agent_contract import Harness
from base.host.env.registry import child_env
from base.lm.harness.bridge import HarnessBridge, HarnessSession, ScopedSessionRegistry
from base.lm.harness.claude import BridgeConfig, ClaudeSession
from base.lm.harness.codex import CodexConfig, CodexSession
from base.lm.harness.handoff import Handoff
from base.lm.harness.registration import HANDLE_ENV, RegistrationStore
from base.lm.harness.registry import SessionRegistry

# meiva:note
# The document a fresh session wakes on. It states who the agent is and where it
# works, and asks for one short acknowledgement: the agent's actual work arrives as
# the session's later messages, so the handoff turn must not start any. The cluster
# sections stay empty here because section 3 (`import meiva`) is true for every
# agent but the entry points a role may reach belong to the role, not to the wake.
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
def open_session(
    harness: Harness,
    *,
    agent_id: int,
    cwd: Path,
    registry: SessionRegistry,
    environ: Mapping[str, str] | None = None,
) -> HarnessSession:
    if harness is Harness.CLAUDE_CODE:
        return ClaudeSession(
            BridgeConfig(cwd=cwd),
            environ=child_env("runner") if environ is None else environ,
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
    raise ValueError(f"no session adapter for harness {harness.value!r}")


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
) -> HarnessBridge | None:
    if harness is Harness.MEIVA:
        return None
    scoped = ScopedSessionRegistry(inner=registry, harness=harness)
    handoff = Handoff(
        agent_id=agent_id,
        instruction=HANDOFF_INSTRUCTION,
        machine=machine,
        workspace=workspace,
    )

    def factory(handle: str) -> HarnessSession:
        inherited = child_env("runner") if environ is None else environ
        return open_session(
            harness,
            agent_id=agent_id,
            cwd=workspace,
            registry=scoped,
            environ={**inherited, HANDLE_ENV: handle},
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
