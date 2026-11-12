# meiva:note
# Shape is not provenance or owner coverage. These cases keep absent/partial/failed readings
# distinct from complete declared-scope measurements, including the live index budget.
from __future__ import annotations

import json

import pytest

from meizel.budgets.report import BudgetRow
from meizel.budgets.snapshot import (
    POOL_KINDS,
    PROVENANCE_FIELDS,
    Coverage,
    Snapshot,
    decode_snapshot,
    render_snapshots,
)


def _provenance() -> dict[str, str]:
    values = {key: "explicit fixture " + key for key in PROVENANCE_FIELDS}
    values.update(
        root="/vault", changed_root="/vault", snapshot="index-123", changed_snapshot="index-123"
    )
    return values


def _pool() -> Snapshot:
    return Snapshot(
        "memory-pool",
        tuple(BudgetRow(kind, "/vault/memory/" + kind, 20, 20, True) for kind in POOL_KINDS),
        POOL_KINDS,
        _provenance(),
    )


def test_complete_scope_preserves_provenance_and_owner_in_both_reports() -> None:
    snapshot = _pool()
    assert snapshot.coverage == Coverage.COMPLETE
    for table in (render_snapshots([snapshot]), render_snapshots([snapshot], attention_only=True)):
        assert "Memory-pool coverage: complete" in table
        assert "1:memory-pool" in table
        assert "index-123" in table and "explicit fixture effective_limits" in table
        assert all(kind in table for kind in POOL_KINDS)
        assert " Source |" in table


def test_no_external_snapshot_is_explicitly_not_provided_without_zero_rows() -> None:
    table = render_snapshots([])
    assert "Memory-pool coverage: not-provided" in table
    assert len([line for line in table.splitlines() if line.startswith("| ")]) == 2


def test_live_index_cannot_be_removed_by_declaring_only_three_expected_kinds() -> None:
    full = _pool()
    snapshot = Snapshot(full.owner, full.rows[:-1], POOL_KINDS[:-1], full.provenance)
    assert snapshot.coverage == Coverage.PARTIAL
    assert snapshot.missing_kinds == ("memory-index-characters",)
    assert "memory-index-characters" in render_snapshots([snapshot])


def test_empty_missing_metadata_and_unavailable_are_distinct() -> None:
    assert decode_snapshot('{"schema":"meizel.budgets/v1","rows":[]}').coverage == Coverage.PARTIAL
    full = _pool()
    assert Snapshot(full.owner, full.rows, POOL_KINDS).coverage == Coverage.PARTIAL
    assert Snapshot(full.owner, (), POOL_KINDS, full.provenance).coverage == Coverage.PARTIAL
    assert (
        Snapshot(full.owner, (), unavailable={"index": "cannot read"}).coverage
        == Coverage.UNAVAILABLE
    )
    assert (
        Snapshot(
            full.owner, full.rows, POOL_KINDS, full.provenance, {"note": "cannot read"}
        ).coverage
        == Coverage.PARTIAL
    )


@pytest.mark.parametrize("field", PROVENANCE_FIELDS)
def test_each_missing_or_unknown_provenance_field_keeps_scope_partial(field: str) -> None:
    full = _pool()
    values = dict(full.provenance)
    values[field] = "not-provided"
    assert Snapshot(full.owner, full.rows, POOL_KINDS, values).coverage == Coverage.PARTIAL


@pytest.mark.parametrize("field", ["changed_root", "changed_snapshot"])
def test_changed_claim_cannot_name_another_root_or_snapshot(field: str) -> None:
    provenance = _provenance()
    provenance[field] = "another"
    with pytest.raises(ValueError, match="does not match"):
        decode_snapshot(
            json.dumps({"schema": "meizel.budgets/v1", "rows": [], "provenance": provenance})
        )


def test_extra_owner_provenance_is_not_discarded() -> None:
    provenance = _provenance()
    provenance["owner-note"] = "measured through staged blobs"
    snapshot = decode_snapshot(
        json.dumps(
            {
                "schema": "meizel.budgets/v1",
                "owner": "memory-pool",
                "rows": [
                    {
                        "kind": "memory-note-characters",
                        "location": "/vault/x.md",
                        "current": 7999,
                        "limit": 8000,
                        "changed": True,
                    }
                ],
                "expected_kinds": list(POOL_KINDS),
                "provenance": provenance,
            }
        )
    )
    assert "measured through staged blobs" in render_snapshots([snapshot], attention_only=True)


def test_template_complete_never_implies_live_owner_was_provided() -> None:
    full = _pool()
    template = Snapshot("memory-template", full.rows[:-1], POOL_KINDS[:-1], full.provenance)
    assert template.coverage == Coverage.COMPLETE
    assert "Memory-pool coverage: not-provided" in render_snapshots([template])
