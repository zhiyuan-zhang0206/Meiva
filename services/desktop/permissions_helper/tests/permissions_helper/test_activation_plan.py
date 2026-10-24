# meiva:note
# The helper activation decisions, as values in and a plan out.

from __future__ import annotations

import pytest

from services.desktop.permissions_helper import activation_plan as plan

_STOP_PROTOCOL = {"root_stop_intent_v1": True, "helper_shutdown_v1": True}


@pytest.mark.parametrize(
    ("stop_intent", "plist_changed", "rebuilt", "refusal"),
    [
        (True, False, False, "retirement is incomplete"),
        (True, True, True, "retirement is incomplete"),
        (False, True, False, "stop and unregister"),
        (False, False, True, "stop and unregister"),
        (False, True, True, "stop and unregister"),
    ],
)
def test_a_loaded_helper_that_must_change_is_refused(
    stop_intent: bool, plist_changed: bool, rebuilt: bool, refusal: str
) -> None:
    result = plan.activation_plan(
        loaded=True, stop_intent=stop_intent, plist_changed=plist_changed, rebuilt=rebuilt
    )

    assert result.refusal is not None and refusal in result.refusal
    assert result.bootstrap is False


def test_a_loaded_helper_that_matches_is_only_observed() -> None:
    result = plan.activation_plan(
        loaded=True, stop_intent=False, plist_changed=False, rebuilt=False
    )

    assert result == plan.Activation(refusal=None, bootstrap=False)


@pytest.mark.parametrize("plist_changed", [False, True])
@pytest.mark.parametrize("rebuilt", [False, True])
def test_an_unloaded_helper_is_bootstrapped(plist_changed: bool, rebuilt: bool) -> None:
    result = plan.activation_plan(
        loaded=False, stop_intent=False, plist_changed=plist_changed, rebuilt=rebuilt
    )

    assert result == plan.Activation(refusal=None, bootstrap=True)


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        (None, "no_answer"),
        ({}, "no_answer"),
        ({"pong": False, **_STOP_PROTOCOL}, "no_answer"),
        ({"pong": True}, "needs_upgrade"),
        ({"pong": True, "root_stop_intent_v1": True}, "needs_upgrade"),
        ({"pong": True, "helper_shutdown_v1": True}, "needs_upgrade"),
        ({"pong": True, **_STOP_PROTOCOL}, "answered"),
    ],
)
def test_a_ping_answer_means_one_verdict(reply: dict[str, object] | None, verdict: str) -> None:
    assert plan.ping_verdict(reply) == verdict


def test_the_plist_pins_the_binary_the_socket_and_a_restart_on_failure() -> None:
    result = plan.helper_plist(
        label="com.meiva.permissions-helper.x",
        executable="/h/Contents/MacOS/MeivaPermissionsHelper",
        log="/logs/permissions-helper.log",
        socket="/run/permissions-helper.1.sock",
        root_seed="/run/seed.json",
    )

    assert result["Label"] == "com.meiva.permissions-helper.x"
    assert result["ProgramArguments"] == ["/h/Contents/MacOS/MeivaPermissionsHelper"]
    assert result["EnvironmentVariables"] == {
        "MEIVA_PERMISSIONS_HELPER_SOCKET": "/run/permissions-helper.1.sock",
        "MEIVA_PERMISSIONS_HELPER_ROOT_SEED": "/run/seed.json",
    }
    assert result["KeepAlive"] == {"SuccessfulExit": False}
    assert (
        result["StandardOutPath"] == result["StandardErrorPath"] == "/logs/permissions-helper.log"
    )


@pytest.mark.parametrize(
    ("plist", "foreign"),
    [
        (
            {"Label": "mine", "EnvironmentVariables": {"MEIVA_PERMISSIONS_HELPER_SOCKET": "/s"}},
            False,
        ),
        (
            {"Label": "stray", "EnvironmentVariables": {"MEIVA_PERMISSIONS_HELPER_SOCKET": "/s"}},
            True,
        ),
        (
            {"Label": "stray", "EnvironmentVariables": {"MEIVA_PERMISSIONS_HELPER_SOCKET": "/o"}},
            False,
        ),
        ({"Label": "stray", "EnvironmentVariables": {}}, False),
        ({"Label": "stray"}, False),
        ({"Label": "stray", "EnvironmentVariables": "not a table"}, False),
    ],
)
def test_only_another_label_on_our_socket_is_foreign(
    plist: dict[str, object], foreign: bool
) -> None:
    assert plan.is_foreign_label_job(plist, label="mine", socket="/s") is foreign


@pytest.mark.parametrize(("length", "too_long"), [(103, False), (104, True), (200, True)])
def test_a_socket_path_must_fit_sun_path(length: int, too_long: bool) -> None:
    assert plan.socket_path_too_long(b"x" * length) is too_long
