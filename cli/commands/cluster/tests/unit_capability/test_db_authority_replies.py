# meiva:note
# What `meiva cluster db-authority issue-unit` and `install-unit` refuse and say.
#
# Which homes each verb refuses and the wording of the refusals and of the report that
# carries the transport key are asserted on values; that the shells print them and exit as
# they say is `test_issue_unit.py` and `test_install_unit.py`.

from __future__ import annotations

import pytest

from cli.commands.cluster.plans.db_authority_replies import (
    IssuedReport,
    failure_line,
    install_refusal,
    issue_refusal,
    issued_text,
)


def test_issue_unit_runs_on_a_gateway_home_with_a_local_data_plane() -> None:
    assert issue_refusal(serves_gateway=True, remote_plane=False) is None


@pytest.mark.parametrize(
    ("serves_gateway", "remote_plane"), [(False, False), (True, True), (False, True)]
)
def test_issue_unit_refuses_a_home_that_is_no_gateway_or_whose_data_plane_is_remote(
    serves_gateway: bool, remote_plane: bool
) -> None:
    refusal = issue_refusal(serves_gateway=serves_gateway, remote_plane=remote_plane)

    assert refusal is not None
    assert refusal.startswith("✗ meiva cluster db-authority issue-unit: runs on a gateway home")
    assert "a remote-managed plane has no write generation to issue" in refusal


def test_install_unit_is_for_agent_runner_units_only() -> None:
    assert install_refusal(frozenset({"agent-runner"})) is None

    refusal = install_refusal(frozenset({"gateway", "agent-runner"}))

    assert refusal is not None
    assert "a gateway unit keeps its own write-generation ledger" in refusal
    assert refusal.startswith("✗ meiva cluster db-authority install-unit: ")


def test_a_failure_names_the_verb_and_the_reason() -> None:
    assert failure_line("install-unit", "no") == "✗ meiva cluster db-authority install-unit: no"


def test_the_report_of_a_sealed_bundle_carries_the_key_and_how_the_unit_installs_it() -> None:
    text = issued_text(
        IssuedReport(
            unit="mini:/home/u/.meiva",
            generation=0,
            expires="2026-10-27T10:00:00+08:00",
            target="/out/mini.bundle",
            transport_key="a2V5",
        )
    )

    first, key, *how = text.splitlines()
    assert first == (
        "✓ bundle for mini:/home/u/.meiva (write generation 0, expires "
        "2026-10-27T10:00:00+08:00) written to /out/mini.bundle (0600)"
    )
    assert key == "  transport key (shown once, carry it separately): a2V5"
    on_the_unit = " ".join(how)
    assert "`.venv/bin/meiva init --db-capability <bundle>`" in on_the_unit
    assert "`meiva cluster db-authority install-unit <bundle>`" in on_the_unit
    assert "export MEIVA_DB_CAPABILITY_KEY from a non-echoing prompt" in on_the_unit
