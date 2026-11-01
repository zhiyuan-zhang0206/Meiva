# meiva:note
# The gateway requests the SDK sends, as plans: values in, request out.
from dataclasses import replace
from typing import cast

import pytest

from meiva.gateway_client.agent_plans import (
    PageIntent,
    close_page_request,
    open_page,
    register_page_request,
    restart_request,
    resurrect_request,
    terminate_request,
)
from meiva.gateway_client.plans import (
    SEND_TIMEOUT_SECONDS,
    NoteIntent,
    Request,
    SpawnIntent,
    list_agents_request,
    message_request,
    patch_label_request,
    spawn_receipt,
    spawn_request,
    system_note_request,
)

BARE = SpawnIntent(
    spawner="agent:1",
    prompt=None,
    fork_from=None,
    prompt_source="agent:1",
    machine=None,
    config=None,
    label=None,
    harness=None,
)


def intent(**changes: object) -> SpawnIntent:
    return replace(BARE, **changes)


def test_a_bare_spawn_names_only_its_spawner() -> None:
    assert spawn_request(intent(), "key") == Request(
        "POST", "/api/keyed/v1/agents", body={"spawner": "agent:1"}, idempotency_key="key"
    )


def test_a_prompt_travels_with_its_source() -> None:
    plan = spawn_request(intent(prompt="goal"), "key")
    assert plan.body == {"spawner": "agent:1", "prompt": "goal", "prompt_source": "agent:1"}


def test_every_given_field_is_sent_and_an_empty_config_still_counts() -> None:
    plan = spawn_request(
        intent(fork_from=4, machine="m", config={}, label="lead", harness="codex"), "key"
    )
    assert plan.body == {
        "spawner": "agent:1",
        "fork_from": 4,
        "machine": "m",
        "config": {},
        "label": "lead",
        "harness": "codex",
    }


@pytest.mark.parametrize("key", ["", "x" * 129, None, 12])
def test_spawn_refuses_an_invalid_key_before_any_plan_exists(key: object) -> None:
    with pytest.raises((TypeError, ValueError), match="idempotency key"):
        spawn_request(intent(), cast("str", key))


def test_a_spawn_receipt_reports_a_withdrawn_model_only_when_normalized() -> None:
    assert spawn_receipt({"id": "7"}).agent_id == 7
    assert spawn_receipt({"id": 7, "config_normalized": None}).withdrawn_model is None
    normalized = spawn_receipt(
        {"id": 7, "config_normalized": {"requested": "old", "resolved": "new"}}
    )
    assert normalized.withdrawn_model == ("old", "new")


def test_a_message_carries_the_long_timeout_its_key_and_the_notice_only_when_set() -> None:
    plain = message_request(
        7, content="hi", source="user", completion_notice=False, idempotency_key="k"
    )
    assert plain == Request(
        "POST",
        "/api/agents/7/messages",
        body={"content": "hi", "source": "user"},
        timeout_seconds=SEND_TIMEOUT_SECONDS,
        idempotency_key="k",
    )
    noticed = message_request(
        7, content="hi", source="user", completion_notice=True, idempotency_key="k"
    )
    assert noticed.body == {"content": "hi", "source": "user", "completion_notice": True}


def test_a_system_note_links_its_task_only_when_given() -> None:
    note = NoteIntent(content="c", note_tag="task", source="agent:1", task_id=None, resurrect=True)
    plan = system_note_request(7, note, "k")
    assert plan.path == "/api/agents/7/system-note"
    assert plan.body == {"content": "c", "note_tag": "task", "source": "agent:1", "resurrect": True}
    assert plan.timeout_seconds == SEND_TIMEOUT_SECONDS
    linked = system_note_request(
        7, NoteIntent("c", "task", "agent:1", task_id=3, resurrect=False), "k"
    )
    assert linked.body is not None and linked.body["task_id"] == 3


def test_a_system_note_refuses_an_invalid_key() -> None:
    note = NoteIntent("c", "task", "agent:1", None, True)
    with pytest.raises(ValueError, match="idempotency key"):
        system_note_request(7, note, "")


def test_a_directory_page_names_its_cursor_only_when_given() -> None:
    first = list_agents_request(scope="live", query="q", before_id=None, limit=50)
    assert first == Request(
        "GET", "/api/agents", params={"scope": "live", "query": "q", "limit": 50}
    )
    later = list_agents_request(scope="all", query="", before_id=90, limit=10)
    assert later.params == {"scope": "all", "query": "", "limit": 10, "before_id": 90}


def test_a_label_change_says_who_set_it() -> None:
    assert patch_label_request(42, "lead", "self") == Request(
        "PATCH", "/api/agents/42", body={"label": "lead", "source": "self"}
    )


def test_terminate_sends_each_flag_only_when_set() -> None:
    quiet = terminate_request(
        7, source="agent:1", message=None, force=False, kill_all_shell_sessions=False
    )
    assert quiet == Request("POST", "/api/agents/7/terminate", body={"source": "agent:1"})
    loud = terminate_request(
        7, source="agent:1", message="bye", force=True, kill_all_shell_sessions=True
    )
    assert loud.body == {
        "source": "agent:1",
        "message": "bye",
        "force": True,
        "kill_all_shell_sessions": True,
    }


def test_restart_and_resurrect_carry_their_provenance() -> None:
    assert restart_request(7, "agent:1", message="reload skill") == Request(
        "POST", "/api/agents/7/restart", body={"source": "agent:1", "message": "reload skill"}
    )
    assert restart_request(
        7, "agent:1", message="switch harness", config_overlay={"harness": "codex"}
    ) == Request(
        "POST",
        "/api/agents/7/restart",
        body={
            "source": "agent:1",
            "message": "switch harness",
            "config_overlay": {"harness": "codex"},
        },
    )
    assert resurrect_request(7, "wake", "agent:1") == Request(
        "POST",
        "/api/agents/7/resurrect",
        body={"prompt": "wake", "resurrected_by": "agent:1"},
    )


def test_a_page_registration_is_keyed_and_sends_only_what_was_given() -> None:
    bare = register_page_request(
        3, PageIntent("main", 8080, "127.0.0.1", None, None, None), "page-key"
    )
    assert bare == Request(
        "POST",
        "/api/keyed/v1/agents/3/pages",
        body={"name": "main", "port": 8080, "host": "127.0.0.1"},
        idempotency_key="page-key",
    )
    full = register_page_request(
        3, PageIntent("main", 8080, "127.0.0.1", "Title", "/srv", 60), "page-key"
    )
    assert full.body == {
        "name": "main",
        "port": 8080,
        "host": "127.0.0.1",
        "title": "Title",
        "serve_dir": "/srv",
        "ttl_seconds": 60,
    }


def test_a_page_is_closed_only_by_the_row_that_was_observed() -> None:
    pages = [{"id": 11, "name": "a"}, {"id": 12, "name": "main"}]
    observed = open_page(pages, "main")
    assert observed == {"id": 12, "name": "main"}
    assert open_page(pages, "gone") is None
    assert observed is not None
    assert close_page_request(3, observed, "close-key") == Request(
        "POST",
        "/api/keyed/v1/agents/3/pages/main/close",
        body={"expected_page_id": 12},
        idempotency_key="close-key",
    )
