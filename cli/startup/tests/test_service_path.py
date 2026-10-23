# meiva:note
# Which host tool directories a new home declares.

from __future__ import annotations

import os

import pytest

from cli.startup.service_path import CallerHost, ServicePathChoice, choose_service_path

_CHECKOUT = "/work/meiva"


def _caller(
    *,
    service_path: str | None = None,
    path: str = "/usr/bin:/bin",
    virtual_env: str | None = None,
    prefix_venv: str | None = None,
) -> CallerHost:
    return CallerHost(service_path, path, virtual_env, prefix_venv, frozenset())


def test_a_declaration_the_home_already_carries_is_admitted_as_written() -> None:
    choice = choose_service_path(
        "/opt/tools",
        home_has_env=True,
        caller=_caller(service_path="/elsewhere"),
        checkout=_CHECKOUT,
    )

    assert choice == ServicePathChoice("/opt/tools", ())


def test_a_home_with_an_env_and_no_declaration_never_recaptures_the_callers_path() -> None:
    with pytest.raises(ValueError, match="explicit MEIVA_SERVICE_PATH declaration"):
        choose_service_path(
            None, home_has_env=True, caller=_caller(service_path="/unadmitted"), checkout=_CHECKOUT
        )


def test_a_new_home_takes_the_service_path_its_caller_declares() -> None:
    choice = choose_service_path(
        None, home_has_env=False, caller=_caller(service_path="/declared"), checkout=_CHECKOUT
    )

    assert choice == ServicePathChoice("/declared", ())


def test_a_new_home_without_a_declaration_takes_the_callers_path_minus_every_virtualenv() -> None:
    caller = _caller(
        path=os.pathsep.join(["/venv/bin", "/usr/bin"]),
        virtual_env="/activated",
        prefix_venv="/interpreter-venv",
    )

    choice = choose_service_path(None, home_has_env=False, caller=caller, checkout=_CHECKOUT)

    assert choice.raw == "/venv/bin:/usr/bin"
    assert choice.excluded == (
        "/work/meiva/.venv/bin",
        "/interpreter-venv/bin",
        "/activated/bin",
    )


def test_only_the_checkouts_virtualenv_is_dropped_when_the_caller_has_no_other() -> None:
    choice = choose_service_path(None, home_has_env=False, caller=_caller(), checkout=_CHECKOUT)

    assert choice == ServicePathChoice("/usr/bin:/bin", ("/work/meiva/.venv/bin",))


def test_an_empty_activated_virtualenv_names_none() -> None:
    choice = choose_service_path(
        None, home_has_env=False, caller=_caller(virtual_env=""), checkout=_CHECKOUT
    )

    assert choice.excluded == ("/work/meiva/.venv/bin",)
