from __future__ import annotations

from base.lm.harness.codex.requests import (
    CLIENT_NAME,
    CLIENT_VERSION,
    INITIALIZED,
    OpenedThread,
    RequestAnswer,
    answer_request,
    initialize_request,
    interrupt_request,
    open_thread_request,
    turn_start_request,
)


def test_the_handshake_names_this_client() -> None:
    assert initialize_request() == (
        "initialize",
        {"clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION}, "capabilities": {}},
    )
    assert (CLIENT_NAME, CLIENT_VERSION, INITIALIZED) == ("meiva-codex-bridge", "1", "initialized")


def test_a_recorded_thread_resumes_with_the_selected_model() -> None:
    assert open_thread_request(
        "thread-old",
        cwd="/elsewhere",
        model="gpt-5-codex",
        approval_policy="untrusted",
        sandbox="read-only",
    ) == ("thread/resume", {"threadId": "thread-old", "model": "gpt-5-codex"})


def test_a_fresh_thread_carries_the_callers_policy_and_inherits_the_environment() -> None:
    method, params = open_thread_request(
        None,
        cwd="/workspace",
        model="gpt-5-codex",
        approval_policy="untrusted",
        sandbox="workspace-write",
    )
    assert method == "thread/start"
    assert params == {
        "cwd": "/workspace",
        "approvalPolicy": "untrusted",
        "sandbox": "workspace-write",
        "config": {"shell_environment_policy": {"inherit": "all"}},
        "model": "gpt-5-codex",
    }


def test_an_unset_model_is_left_to_the_accounts_own_default() -> None:
    _, params = open_thread_request(
        None,
        cwd="/workspace",
        model=None,
        approval_policy="never",
        sandbox="danger-full-access",
    )
    assert "model" not in params


def test_a_turn_text_is_sent_as_one_text_input() -> None:
    assert turn_start_request("thread-1", "do the thing") == (
        "turn/start",
        {"threadId": "thread-1", "input": [{"type": "text", "text": "do the thing"}]},
    )


def test_an_interrupt_names_the_thread_and_the_providers_turn() -> None:
    assert interrupt_request("thread-1", "codex-turn-9") == (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "codex-turn-9"},
    )


def test_an_accepted_command_is_answered_with_the_configured_decision() -> None:
    answer = answer_request(
        "item/commandExecution/requestApproval", {"command": "rm -rf build"}, decision="accept"
    )
    assert answer == RequestAnswer({"decision": "accept"})


def test_a_declined_command_is_answered_and_recorded_as_a_denial() -> None:
    answer = answer_request(
        "item/commandExecution/requestApproval", {"command": "rm -rf build"}, decision="decline"
    )
    assert answer.answer == {"decision": "decline"}
    assert answer.denial == "command: rm -rf build"


def test_the_legacy_patch_approval_keeps_its_own_vocabulary() -> None:
    accepted = answer_request("applyPatchApproval", {"callId": "call-1"}, decision="accept")
    assert (accepted.answer, accepted.denial) == ({"decision": "approved"}, None)
    declined = answer_request("applyPatchApproval", {"callId": "call-1"}, decision="decline")
    assert (declined.answer, declined.denial) == ({"decision": "denied"}, "applyPatchApproval")


def test_permissions_and_user_input_are_answered_empty() -> None:
    assert answer_request("item/permissions/requestApproval", {}, decision="accept").answer == {
        "permissions": {},
        "scope": "turn",
    }
    assert answer_request("item/tool/requestUserInput", {}, decision="accept").answer == {
        "answers": {}
    }
    unknown = answer_request("item/somethingElse", {}, decision="accept")
    assert (unknown.answer, unknown.denial) == ({}, None)


def test_an_opened_thread_reads_its_id_model_and_directory() -> None:
    opened = OpenedThread.of(
        {"thread": {"id": "thread-1", "cwd": "/app"}, "model": "gpt-5-codex"},
        resume=None,
        model="configured",
        cwd="/workspace",
    )
    assert opened == OpenedThread(session_id="thread-1", model="gpt-5-codex", cwd="/app")


def test_a_resume_that_names_no_id_keeps_the_one_it_resumed() -> None:
    opened = OpenedThread.of(
        {"thread": {}}, resume="thread-old", model="configured", cwd="/workspace"
    )
    assert (opened.session_id, opened.model, opened.cwd) == (
        "thread-old",
        "configured",
        "/workspace",
    )


def test_a_fresh_thread_that_names_no_id_stays_empty() -> None:
    assert OpenedThread.of({}, resume=None, model="configured", cwd="/workspace").session_id == ""
