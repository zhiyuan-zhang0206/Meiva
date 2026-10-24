# meiva:note
# The pure classification helpers of cluster status: schema comparison and the wording of
# unreadable schema evidence, session counting and grouping.

from __future__ import annotations

import pytest

from ops.cluster_status.model import (
    SchemaReadFailure,
    classify,
    count_agent_shells,
    group_agent_sessions,
    read_failure_mismatch,
)
from ops.rpc_schemas import SessionInfo


@pytest.mark.parametrize(
    ("applied", "required", "kind", "detail"),
    [
        ({"base"}, {"base"}, None, None),
        (
            {"old"},
            {"new"},
            "divergent",
            "DB has 1 migration(s) this image lacks and lacks 1 required by this image",
        ),
        (
            {"base", "new"},
            {"base"},
            "schema-ahead-of-code",
            "DB has 1 migration(s) this image lacks",
        ),
        (
            {"base"},
            {"base", "new", "newer"},
            "schema-behind-code",
            "DB lacks 2 migration(s) required by this image",
        ),
    ],
)
def test_the_two_migration_sets_decide_the_diagnosis(
    applied: set[str], required: set[str], kind: str | None, detail: str | None
) -> None:
    mismatch = classify(applied, required)

    if kind is None:
        assert mismatch is None
    else:
        assert mismatch is not None
        assert (mismatch.kind, mismatch.detail) == (kind, detail)


@pytest.mark.parametrize(
    ("failure", "kind", "detail"),
    [
        (
            SchemaReadFailure.DB_CATALOG_INVALID,
            "invalid-migration-layout",
            "DB migration catalog is invalid: unrecognized shape",
        ),
        (
            SchemaReadFailure.IMAGE_LAYOUT_INVALID,
            "invalid-migration-layout",
            "image migration layout is invalid: unrecognized shape",
        ),
        (
            SchemaReadFailure.DB_UNAVAILABLE,
            "unavailable",
            "applied migration set unavailable (unrecognized shape)",
        ),
    ],
)
def test_evidence_that_cannot_be_read_is_not_a_set_divergence(
    failure: SchemaReadFailure, kind: str, detail: str
) -> None:
    mismatch = read_failure_mismatch(failure, "unrecognized shape")

    assert (mismatch.kind, mismatch.detail) == (kind, detail)


def _rows(*names: str) -> list[SessionInfo]:
    return [SessionInfo(name=name) for name in names]


def test_agent_shells_and_watchers_are_counted_but_services_are_not() -> None:
    sessions = _rows(
        "meiva-main-agent-7-shell-0",
        "meiva-main-agent-7-shell-0-watcher",
        "meiva-main-agent-12-shell-3",
        "meiva-main-restarter",
    )

    assert count_agent_shells(sessions) == 3
    assert count_agent_shells([]) == 0


def test_sessions_group_by_agent_and_services_form_no_group() -> None:
    groups = group_agent_sessions(
        _rows(
            "meiva-agent-7-shell-0",
            "meiva-agent-12-shell-3",
            "meiva-agent-7-shell-0-watcher",
            "meiva-gateway",
        )
    )

    assert [(g.agent_id, [s.name for s in g.shells]) for g in groups] == [
        (7, ["meiva-agent-7-shell-0", "meiva-agent-7-shell-0-watcher"]),
        (12, ["meiva-agent-12-shell-3"]),
    ]
