# meiva:note
# The machine dimension is `{canonical name} | unknown`, and the four hostnames already in
# `audit_events` are mapped from data, not from a guess. These tests pin both halves: the
# mapping itself, and the rule that a value the mapping does not know is passed through
# unchanged instead of being folded into a machine it might not be.

from __future__ import annotations

from pathlib import Path

import pytest

from base.telemetry.machine import UNKNOWN_MACHINE, canonical_machine
from base.telemetry.machine.aliases import EVIDENCE
from base.telemetry.machine.identity import machine_aliases

_MACHINE = Path(__file__).resolve().parents[1]
_MIGRATION = (
    _MACHINE.parents[2] / "migrations/2026/11/04/18/20261104T181300_audit-machine-canonical.sql"
)


def test_every_recorded_hostname_maps_to_its_measured_machine() -> None:
    measured = {
        "Zyons-Mac-mini.local": "macmini",
        "zhangzhiyuandeMac-mini.local": "company-mini",
        "Zhiyuans-MacBook-Air-606.local": "macbook-air",
        "zhiyuanzhangs-MacBook-Air.local": "company-air",
    }
    assert machine_aliases() == measured


@pytest.mark.parametrize("raw", ["wsl", "win", "corpmac", "unknown", "some-new-host.local"])
def test_a_value_the_mapping_does_not_know_passes_through(raw: str) -> None:
    result = canonical_machine(raw)
    assert result.value == raw
    assert result.mapped is False


def test_empty_reads_as_unknown_and_is_never_guessed() -> None:
    assert canonical_machine("").value == UNKNOWN_MACHINE
    assert canonical_machine("   ").value == UNKNOWN_MACHINE


def test_every_mapping_carries_its_evidence() -> None:
    from base.telemetry.machine import MACHINES

    assert set(EVIDENCE) == set(MACHINES)
    for hostname, entry in EVIDENCE.items():
        assert entry["machine"] == MACHINES[hostname], hostname
        assert entry["source"], hostname
        assert entry["caller_user"], hostname
        assert entry["verified_at"], hostname


def test_the_view_maps_exactly_the_rows_the_data_file_maps() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8").lower()
    for fqdn, alias in machine_aliases().items():
        assert f"when '{fqdn.lower()}' then '{alias.lower()}'" in sql, fqdn
