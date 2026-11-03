# meiva:note
# The warnings a gateway prints when a security switch was never chosen.

from __future__ import annotations

from pathlib import Path

from gateway.cluster.startup_warnings import declared_env_keys, startup_warnings


def _env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_loopback_listener_says_nothing_about_either_switch(tmp_path: Path) -> None:
    # meiva:note
    # Both defaults are the intended single-box posture behind loopback; a warning on a
    # healthy start is one nobody reads.
    env = _env(tmp_path, "MEIVA_MACHINE_NAME=box\n")
    assert (
        startup_warnings(
            human_auth="trusted_network",
            alerts_token_configured=False,
            env_path=env,
            bind_host="127.0.0.1",
        )
        == []
    )


def test_an_unset_human_auth_switch_is_named_before_the_listener_binds(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, "MEIVA_MACHINE_NAME=box\n")
    warnings = startup_warnings(
        human_auth="trusted_network",
        alerts_token_configured=True,
        env_path=env,
        bind_host="",
    )
    assert len(warnings) == 1
    assert "MEIVA_GATEWAY_HUMAN_AUTH" in warnings[0]
    assert str(env) in warnings[0]
    assert "required" in warnings[0]


def test_an_operator_who_wrote_trusted_network_is_not_warned_again(tmp_path: Path) -> None:
    # meiva:note
    # The warning exists for the key nobody wrote. A declared choice, even the open one,
    # is a decision this process must not second-guess on every start.
    env = _env(tmp_path, "MEIVA_GATEWAY_HUMAN_AUTH=trusted_network\n")
    assert (
        startup_warnings(
            human_auth="trusted_network",
            alerts_token_configured=True,
            env_path=env,
            bind_host="",
        )
        == []
    )


def test_a_required_human_auth_switch_is_never_warned_about(tmp_path: Path) -> None:
    env = _env(tmp_path, "")
    warnings = startup_warnings(
        human_auth="required",
        alerts_token_configured=True,
        env_path=env,
        bind_host="",
    )
    assert warnings == []


def test_a_missing_alerts_token_is_named_with_what_it_admits(tmp_path: Path) -> None:
    env = _env(tmp_path, "MEIVA_GATEWAY_HUMAN_AUTH=required\n")
    warnings = startup_warnings(
        human_auth="required",
        alerts_token_configured=False,
        env_path=env,
        bind_host="",
    )
    assert len(warnings) == 1
    assert "MEIVA_ALERTS_WEBHOOK_TOKEN" in warnings[0]
    assert "/api/alerts" in warnings[0]


def test_both_switches_left_open_produce_both_lines(tmp_path: Path) -> None:
    env = _env(tmp_path, "")
    assert (
        len(
            startup_warnings(
                human_auth="trusted_network",
                alerts_token_configured=False,
                env_path=env,
                bind_host="",
            )
        )
        == 2
    )


def test_a_missing_env_file_warns_rather_than_failing(tmp_path: Path) -> None:
    assert (
        startup_warnings(
            human_auth="trusted_network",
            alerts_token_configured=False,
            env_path=tmp_path / "absent.env",
            bind_host="",
        )
        != []
    )


def test_declared_keys_read_every_form_the_environment_file_allows(
    tmp_path: Path,
) -> None:
    env = _env(
        tmp_path,
        "# a comment\n"
        "\n"
        "export MEIVA_GATEWAY_HUMAN_AUTH=required\n"
        "MEIVA_ALERTS_WEBHOOK_TOKEN=\n"
        "SPACED = value\n"
        "NOT_A_KEY\n",
    )
    assert declared_env_keys(env) == frozenset(
        {"MEIVA_GATEWAY_HUMAN_AUTH", "MEIVA_ALERTS_WEBHOOK_TOKEN", "SPACED"}
    )
