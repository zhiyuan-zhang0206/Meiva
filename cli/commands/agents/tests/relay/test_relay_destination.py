# meiva:note
# Which relay options name a destination, as values. The CLI boundary and the relay's own
# startup read one rule, so the same combination is refused the same way in both.

from __future__ import annotations

from uuid import UUID

import pytest

from cli.commands.agents.relay.destination import (
    AntigravityConversation,
    ClaudeSession,
    CodexThread,
    DshSession,
    plan_destination,
    relay_spec_problem,
)

THREAD = "b9d32d0d-bd27-40fc-83e8-692769b21523"
SOCKET = "unix:///tmp/meiva-codex.sock"


def _problem(
    provider: str,
    thread_id: str | None = None,
    codex_remote: str | None = None,
    env_thread_id: str | None = None,
) -> str | None:
    return relay_spec_problem(provider, thread_id, codex_remote, env_thread_id=env_thread_id)


@pytest.mark.parametrize(
    ("provider", "thread_id", "codex_remote"),
    [
        ("codex", THREAD, None),
        ("codex", THREAD, SOCKET),
        ("codex", THREAD, "ws://127.0.0.1:4500"),
        ("antigravity", THREAD, None),
        ("claude", None, None),
        ("dsh", None, None),
    ],
)
def test_a_complete_combination_names_a_destination(
    provider: str, thread_id: str | None, codex_remote: str | None
) -> None:
    assert _problem(provider, thread_id, codex_remote) is None


def test_a_host_relay_needs_the_session_it_delivers_into() -> None:
    assert "needs --thread-id" in (_problem("codex") or "")
    assert "needs --thread-id" in (_problem("antigravity") or "")


def test_a_request_may_take_the_antigravity_thread_from_the_host_environment() -> None:
    assert _problem("antigravity", None, None, env_thread_id=THREAD) is None
    assert "ANTIGRAVITY_CONVERSATION_ID" in (_problem("antigravity") or "")


def test_a_codex_remote_must_be_a_unix_or_ws_endpoint() -> None:
    assert "unix:// or ws://" in (_problem("codex", "t", "http://bad") or "")


@pytest.mark.parametrize("provider", ["claude", "dsh", "antigravity"])
def test_controller_adapter_rejects_codex_remote(provider: str) -> None:
    thread_id = THREAD if provider == "antigravity" else None
    assert "--codex-remote" in (_problem(provider, thread_id, SOCKET) or "")


@pytest.mark.parametrize("provider", ["claude", "dsh"])
def test_a_controller_session_routes_to_its_owner(provider: str) -> None:
    assert "routes to its owner" in (_problem(provider, "thread") or "")


def test_an_unknown_provider_names_nothing() -> None:
    assert "unknown relay provider" in (_problem("?") or "")


def test_each_provider_plans_its_own_destination() -> None:
    assert (
        plan_destination("claude", None, codex_remote=None, endpoint_record=None) == ClaudeSession()
    )
    assert plan_destination("dsh", None, codex_remote=None, endpoint_record=None) == DshSession()
    assert plan_destination(
        "codex", THREAD, codex_remote=SOCKET, endpoint_record=None
    ) == CodexThread(UUID(THREAD), SOCKET)
    assert plan_destination(
        "antigravity", THREAD, codex_remote=None, endpoint_record="/p/record.json"
    ) == AntigravityConversation(UUID(THREAD), "/p/record.json")


@pytest.mark.parametrize(
    ("provider", "thread_id", "codex_remote"),
    [
        ("codex", None, None),
        ("antigravity", None, None),
        ("claude", "thread", None),
        ("dsh", "thread", None),
        ("claude", None, SOCKET),
        ("antigravity", THREAD, SOCKET),
        ("?", None, None),
    ],
)
def test_host_target_must_be_explicit(
    provider: str, thread_id: str | None, codex_remote: str | None
) -> None:
    with pytest.raises(ValueError):
        plan_destination(provider, thread_id, codex_remote=codex_remote, endpoint_record=None)


def test_a_thread_that_is_not_a_uuid_is_refused_before_any_lease_work() -> None:
    with pytest.raises(ValueError, match="badly formed"):
        plan_destination("codex", "thread-1", codex_remote=None, endpoint_record=None)
