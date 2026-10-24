# meiva:note
# What `meiva impersonate request` settles before it asks for a lease, and what it says after,
# as values.

from __future__ import annotations

from pathlib import PurePath
from uuid import UUID

import pytest

from cli.commands.agents.relay.request_plan import (
    Handoff,
    RequestPlan,
    handoff_hint,
    plan_request,
    relay_stub_text,
)

THREAD = "B9D32D0D-BD27-40FC-83E8-692769B21523"
CREDENTIAL = "tok-9"


def _plan(
    provider: str,
    thread_id: str | None = None,
    codex_remote: str | None = None,
    conversation_env: str | None = None,
    dsh_stub_env: str | None = None,
) -> RequestPlan:
    return plan_request(
        provider,
        thread_id,
        codex_remote,
        conversation_env=conversation_env,
        dsh_stub_env=dsh_stub_env,
    )


def test_a_dsh_request_without_the_plugin_is_refused_before_any_lease() -> None:
    with pytest.raises(ValueError, match="DSH_MEIVA_RELAY_STUB is unset"):
        _plan("dsh")
    with pytest.raises(ValueError, match="DSH_MEIVA_RELAY_STUB is unset"):
        _plan("dsh", dsh_stub_env="")


def test_a_dsh_request_names_the_stub_its_plugin_exports() -> None:
    plan = _plan("dsh", dsh_stub_env="/run/dsh/session-1.env")
    assert plan == RequestPlan(Handoff.DSH, None, None, PurePath("/run/dsh/session-1.env"))


def test_an_antigravity_request_takes_its_thread_from_the_host_when_not_given() -> None:
    plan = _plan("antigravity", conversation_env=f"  {THREAD}\n")
    assert plan.thread_id == str(UUID(THREAD))
    assert plan.handoff is Handoff.ANTIGRAVITY


def test_an_antigravity_thread_given_on_the_command_line_wins_and_is_normalized() -> None:
    plan = _plan("antigravity", thread_id=THREAD, conversation_env=str(UUID(int=1)))
    assert plan.thread_id == str(UUID(THREAD))


@pytest.mark.parametrize("env", [None, "", "   "])
def test_an_antigravity_request_with_no_conversation_is_refused(env: str | None) -> None:
    with pytest.raises(ValueError, match="--thread-id or ANTIGRAVITY_CONVERSATION_ID"):
        _plan("antigravity", conversation_env=env)


def test_an_antigravity_thread_that_is_not_a_uuid_is_refused() -> None:
    with pytest.raises(ValueError, match="badly formed"):
        _plan("antigravity", thread_id="conversation-1")


def test_a_codex_request_keeps_the_endpoint_it_was_given() -> None:
    plan = _plan("codex", thread_id="thread-1", codex_remote="unix:///tmp/codex.sock")
    assert plan == RequestPlan(Handoff.CODEX, "thread-1", "unix:///tmp/codex.sock", None)


def test_a_claude_request_needs_no_host_facts() -> None:
    assert _plan("claude") == RequestPlan(Handoff.CLAUDE, None, None, None)


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown relay provider"):
        _plan("?")


def test_the_credential_stub_is_the_three_lines_the_plugin_loads() -> None:
    assert relay_stub_text(agent_id=405, session_id=9, token=CREDENTIAL, interpreter=None) == (
        f"SID=9\nAGENT=405\nMEIVA_IMPERSONATION_RELAY_TOKEN={CREDENTIAL}\n"
    )


def test_the_credential_stub_names_the_interpreter_the_relay_runs_under() -> None:
    stub = relay_stub_text(
        agent_id=405, session_id=9, token=CREDENTIAL, interpreter="/venv/bin/python"
    )
    assert stub.endswith("MEIVA_IMPERSONATION_RELAY_PY=/venv/bin/python\n")


def test_each_handoff_tells_the_operator_how_its_relay_starts() -> None:
    claude = handoff_hint(_plan("claude"))
    assert "Start the claude relay" in claude
    assert "MEIVA_IMPERSONATION_RELAY_TOKEN" in claude
    assert "relay token printed above" in claude

    dsh = handoff_hint(_plan("dsh", dsh_stub_env="/run/dsh/s.env"))
    assert "credential stub (/run/dsh/s.env)" in dsh

    assert "starts the codex relay automatically" in handoff_hint(_plan("codex", "t"))
    assert "pass --codex-remote" in handoff_hint(_plan("codex", "t"))
    assert "starts the antigravity relay automatically" in handoff_hint(
        _plan("antigravity", thread_id=THREAD)
    )
