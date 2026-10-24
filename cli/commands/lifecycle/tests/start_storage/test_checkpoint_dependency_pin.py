# meiva:note
# The checkpoint-schema dependency pin that gates `meiva start`'s migration step.
#
# `base.cluster.provision.assert_checkpoint_dependency_pinned` refuses a LangGraph upgrade that
# added a checkpoint migration no Meiva migration mirrors. It takes the pinned manifest and the
# upstream migration list as values; the start step passes the module's manifest and the
# installed dependency's list, and these tests pass the values they want.

from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster import provision
from base.cluster.checkpoint_schema import PostgresCheckpointSchema
from base.deploy.schema.migration_layout import required_migration_set
from base.deploy.schema.tests.migration_support import git, init_repo

_UPSTREAM = PostgresCheckpointSchema().migrations


def test_checkpoint_schema_upstream_baseline_is_frozen() -> None:
    assert provision.CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION == 9
    assert provision.CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS == {}


def test_checkpoint_migration_manifest_must_be_contiguous() -> None:
    with pytest.raises(provision.CheckpointDependencyDriftError, match="contiguous"):
        provision.assert_checkpoint_dependency_pinned(
            set(), manifest={11: "future"}, upstream_migrations=_UPSTREAM
        )


def test_checkpoint_migration_manifest_follows_upstream_version_order() -> None:
    manifest = {
        10: "20990102T000000_checkpoint-v10",
        11: "20990101T000000_checkpoint-v11",
    }

    with pytest.raises(provision.CheckpointDependencyDriftError, match="version order"):
        provision.assert_checkpoint_dependency_pinned(
            set(), manifest=manifest, upstream_migrations=_UPSTREAM
        )


@pytest.mark.parametrize(
    ("tracked", "write_up"),
    [(False, True), (True, False)],
    ids=["untracked", "missing-up"],
)
def test_checkpoint_migration_manifest_requires_a_tracked_migration(
    tmp_path: Path, *, tracked: bool, write_up: bool
) -> None:
    init_repo(tmp_path)
    name = "20990101T000000_checkpoint-v10"
    if write_up:
        (tmp_path / f"{name}.sql").write_text("SELECT 1;\n")
    if tracked:
        git(tmp_path, "add", "-A")

    with pytest.raises(
        provision.CheckpointDependencyDriftError, match="git-tracked Meiva migration"
    ):
        provision.assert_checkpoint_dependency_pinned(
            required_migration_set(migrations_dir=tmp_path),
            manifest={10: name},
            upstream_migrations=_UPSTREAM,
        )


def test_checkpoint_migration_manifest_accepts_nested_tracked_sql(tmp_path: Path) -> None:
    name = "20990101T000000_checkpoint-v10"
    hour = tmp_path / "2099/01/01/00"
    hour.mkdir(parents=True)
    (hour / f"{name}.sql").write_text("SELECT 1;\n")
    init_repo(tmp_path)

    provision.assert_checkpoint_dependency_pinned(
        required_migration_set(migrations_dir=tmp_path),
        manifest={10: name},
        upstream_migrations=[*_UPSTREAM, "SELECT 1;"],
    )
