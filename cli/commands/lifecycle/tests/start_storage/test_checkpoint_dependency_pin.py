# meiva:note
# The checkpoint-schema dependency pin that gates `meiva start`'s migration step.
#
# `base.cluster.provision.assert_checkpoint_dependency_pinned` refuses a LangGraph upgrade that added
# a checkpoint migration no Meiva migration mirrors. It reads the pinned manifest and the upstream
# migration list as module constants, so these tests replace the constants; the function belongs to
# `base.cluster` and takes no values for them (see the report's "needs base").

from __future__ import annotations

from pathlib import Path

import pytest


def test_checkpoint_schema_upstream_baseline_is_frozen() -> None:
    from base.cluster.provision import (
        CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS,
        CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION,
    )

    assert CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION == 9
    assert CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS == {}


def test_checkpoint_migration_manifest_must_be_contiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster import provision

    monkeypatch.setattr(provision, "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS", {11: "future"})

    with pytest.raises(provision.CheckpointDependencyDriftError, match="contiguous"):
        provision.assert_checkpoint_dependency_pinned(set())


def test_checkpoint_migration_manifest_follows_upstream_version_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster import provision

    monkeypatch.setattr(
        provision,
        "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS",
        {
            10: "20990102T000000_checkpoint-v10",
            11: "20990101T000000_checkpoint-v11",
        },
    )

    with pytest.raises(provision.CheckpointDependencyDriftError, match="version order"):
        provision.assert_checkpoint_dependency_pinned(set())


@pytest.mark.parametrize(
    ("tracked", "write_up"),
    [(False, True), (True, False)],
    ids=["untracked", "missing-up"],
)
def test_checkpoint_migration_manifest_requires_a_tracked_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tracked: bool,
    write_up: bool,
) -> None:
    from base.cluster import provision
    from base.deploy.schema.migration_layout import required_migration_set
    from base.deploy.schema.tests.migration_support import git, init_repo

    init_repo(tmp_path)
    name = "20990101T000000_checkpoint-v10"
    if write_up:
        (tmp_path / f"{name}.sql").write_text("SELECT 1;\n")
    monkeypatch.setattr(provision, "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS", {10: name})
    if tracked:
        git(tmp_path, "add", "-A")

    with pytest.raises(
        provision.CheckpointDependencyDriftError, match="git-tracked Meiva migration"
    ):
        provision.assert_checkpoint_dependency_pinned(
            required_migration_set(migrations_dir=tmp_path)
        )


def test_checkpoint_migration_manifest_accepts_nested_tracked_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    from base.cluster import provision
    from base.deploy.schema.migration_layout import required_migration_set
    from base.deploy.schema.tests.migration_support import init_repo

    name = "20990101T000000_checkpoint-v10"
    hour = tmp_path / "2099/01/01/00"
    hour.mkdir(parents=True)
    (hour / f"{name}.sql").write_text("SELECT 1;\n")
    init_repo(tmp_path)
    monkeypatch.setattr(provision, "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS", {10: name})
    monkeypatch.setattr(PostgresSaver, "MIGRATIONS", [*PostgresSaver.MIGRATIONS, "SELECT 1;"])

    provision.assert_checkpoint_dependency_pinned(required_migration_set(migrations_dir=tmp_path))
