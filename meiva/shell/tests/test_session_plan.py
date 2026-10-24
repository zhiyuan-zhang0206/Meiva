# meiva:note
# What a session may be called and live for, how ids map onto backend names, and the environment a session starts with.
from __future__ import annotations

from pathlib import Path

import pytest

from meiva.shell.session_env import SessionHost, session_environment
from meiva.shell.session_plan import (
    MAX_TTL_SECONDS,
    match_session,
    own_names,
    session_full_name,
    session_table,
    validate_name,
    validate_ttl,
)

PREFIX = "meiva-agent-7-shell-"


def test_validate_ttl_accepts_exactly_max() -> None:
    assert validate_ttl(float(MAX_TTL_SECONDS)) == 86400.0
    with pytest.raises(ValueError, match="ttl"):
        validate_ttl(86400.001)


@pytest.mark.parametrize("ttl", [0.0, -1.0, float("inf"), float("nan")])
def test_validate_ttl_refuses_a_deadline_that_is_not_in_the_future(ttl: float) -> None:
    with pytest.raises(ValueError, match="finite and greater than zero"):
        validate_ttl(ttl)
    with pytest.raises(ValueError, match="finite and greater than zero"):
        validate_ttl(ttl, system=True)


def test_a_system_session_is_exempt_from_the_user_cap() -> None:
    assert validate_ttl(7 * 86400.0, system=True) == 7 * 86400.0
    with pytest.raises(ValueError, match="at most 86400 seconds"):
        validate_ttl(7 * 86400.0)


@pytest.mark.parametrize("name", ["Dev Server", "1abc", "-x", "a_b", ""])
def test_a_name_must_be_a_lowercase_slug(name: str) -> None:
    with pytest.raises(ValueError, match="lowercase slug"):
        validate_name(name, page_label=False)


def test_page_names_are_reserved_for_pages() -> None:
    validate_name("dev-server", page_label=False)
    validate_name(None, page_label=False)
    with pytest.raises(ValueError, match="reserved for pages"):
        validate_name("page-preview", page_label=True)


def test_full_name_carries_the_id_and_the_optional_label() -> None:
    assert session_full_name(PREFIX, 3, None) == f"{PREFIX}3"
    assert session_full_name(PREFIX, 3, "build") == f"{PREFIX}3-build"


def test_own_names_keeps_the_agents_sessions_only() -> None:
    names = [f"{PREFIX}1", "meiva-agent-8-shell-1", "meiva-pty-sessions", f"{PREFIX}2-watcher"]
    assert own_names(names, PREFIX) == [f"{PREFIX}1", f"{PREFIX}2-watcher"]


def test_resolve_does_not_conflate_id_prefixes() -> None:
    names = [f"{PREFIX}12-watcher", f"{PREFIX}1"]
    assert match_session(names, PREFIX, 1) == f"{PREFIX}1"
    assert match_session(names, PREFIX, 12) == f"{PREFIX}12-watcher"


def test_a_foreign_id_does_not_resolve() -> None:
    with pytest.raises(ValueError, match="not this agent's"):
        match_session([f"{PREFIX}1", "meiva-agent-8-shell-2"], PREFIX, 2)


def test_the_table_maps_ids_to_display_names() -> None:
    names = [f"{PREFIX}0", f"{PREFIX}5-dev-server", "meiva-agent-8-shell-9-other"]
    assert session_table(names, PREFIX) == {0: None, 5: "dev-server"}


def _host(*, with_venv: bool = True) -> SessionHost:
    forwarded = {"PATH": "/venv/bin:/usr/bin", "MEIVA_HOME": "/home/x"}
    if with_venv:
        forwarded["VIRTUAL_ENV"] = "/venv"
    return SessionHost(checkout=Path("/checkout"), forwarded=forwarded)


def test_a_session_inside_the_checkout_activates_its_venv() -> None:
    env = session_environment(_host(), activate_venv=True, agent_id=41, overrides=None)
    assert env["VIRTUAL_ENV"] == "/venv"
    assert env["PATH"] == "/venv/bin:/usr/bin"


def test_a_session_outside_the_checkout_keeps_the_path_but_not_the_venv() -> None:
    env = session_environment(_host(), activate_venv=False, agent_id=41, overrides=None)
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"] == "/venv/bin:/usr/bin"


def test_every_session_names_its_owner() -> None:
    env = session_environment(_host(), activate_venv=True, agent_id=41, overrides=None)
    assert env["MEIVA_AGENT_ID"] == "41"
    assert env["MEIVA_HOME"] == "/home/x"


def test_overrides_win_and_the_forwarded_mapping_is_untouched() -> None:
    host = _host()
    env = session_environment(
        host, activate_venv=False, agent_id=41, overrides={"MEIVA_DB_URL": "u", "PATH": "/p"}
    )
    assert env["MEIVA_DB_URL"] == "u"
    assert env["PATH"] == "/p"
    assert dict(host.forwarded) == {
        "PATH": "/venv/bin:/usr/bin",
        "MEIVA_HOME": "/home/x",
        "VIRTUAL_ENV": "/venv",
    }
