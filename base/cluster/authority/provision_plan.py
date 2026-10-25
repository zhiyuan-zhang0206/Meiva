from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from types import MappingProxyType
from typing import Literal

# meiva:note
# Frozen at the upstream schema present when Meiva adopted mirrored checkpoint
# migrations. Never bump this baseline: future versions belong in the manifest
# below and must name their mirroring Meiva migration.
CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION = 9
CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS: Mapping[int, str] = MappingProxyType({})


class CheckpointSchemaError(RuntimeError):
    pass


class CheckpointDependencyDriftError(CheckpointSchemaError):
    pass


class CheckpointSchemaMismatchError(CheckpointSchemaError):
    pass


CheckpointAction = Literal["noop", "verify_only", "setup"]
ProvisionAction = Literal["noop", "create_and_apply", "resume_apply"]


def _check_checkpoint_manifest_shape(manifest: Mapping[int, str]) -> list[int]:
    declared_versions = sorted(manifest)
    expected_declared = list(
        range(
            CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION + 1,
            CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION + 1 + len(declared_versions),
        )
    )
    if declared_versions != expected_declared:
        raise CheckpointDependencyDriftError(
            "checkpoint migration manifest must be contiguous after the frozen "
            f"upstream baseline {CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION}: "
            f"declared={declared_versions}, expected={expected_declared}"
        )

    declared_names = [manifest[version] for version in declared_versions]
    if declared_names != sorted(declared_names):
        raise CheckpointDependencyDriftError(
            "checkpoint migration manifest names must follow upstream version order: "
            f"declared={declared_names}, sorted={sorted(declared_names)}"
        )
    return declared_versions


def expected_checkpoint_schema_versions(
    upstream_migrations: Sequence[str], manifest: Mapping[int, str]
) -> frozenset[int]:
    declared_versions = _check_checkpoint_manifest_shape(manifest)
    approved_target = CHECKPOINT_SCHEMA_UPSTREAM_BASELINE_VERSION + len(declared_versions)
    dependency_target = len(upstream_migrations) - 1
    if dependency_target != approved_target:
        raise CheckpointDependencyDriftError(
            "LangGraph checkpoint migrations changed without an Meiva migration: "
            f"dependency target={dependency_target}, "
            f"approved target={approved_target}. Mirror every new "
            "upstream migration in an Meiva timestamp migration (including the "
            "checkpoint_migrations row), then add it to "
            "CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS."
        )
    return frozenset(range(approved_target + 1))


def verify_checkpoint_dependency_pinned(
    tracked_migrations: Collection[str],
    *,
    manifest: Mapping[int, str],
    upstream_migrations: Sequence[str],
) -> None:
    _check_checkpoint_manifest_shape(manifest)
    for version, name in manifest.items():
        if name not in tracked_migrations:
            raise CheckpointDependencyDriftError(
                f"checkpoint version {version} must name a git-tracked Meiva "
                f"migration: name={name!r}, tracked={name in tracked_migrations}"
            )
    expected_checkpoint_schema_versions(upstream_migrations, manifest)


def verify_checkpoint_schema_versions(
    actual: frozenset[int] | None,
    expected: frozenset[int],
) -> None:
    if actual != expected:
        found: frozenset[int] = frozenset() if actual is None else actual
        raise CheckpointSchemaMismatchError(
            "checkpoint schema is not current: "
            f"missing={sorted(expected - found)}, unexpected={sorted(found - expected)}, "
            f"table_present={actual is not None}. Run `meiva start --all-services` (or this unit's own service selection) on the gateway; "
            "do not grant schema CREATE to runtime roles."
        )


def plan_checkpoint_schema(
    actual: frozenset[int] | None,
    expected: frozenset[int],
    *,
    database_created: bool = False,
    resume_partial: bool = False,
) -> CheckpointAction:
    if actual == expected:
        return "noop"
    may_setup = database_created or resume_partial
    resumable = actual is None or actual == frozenset(range(len(actual)))
    if not may_setup or not resumable:
        return "verify_only"
    return "setup"


def plan_database_provision(
    identity: str,
    *,
    db_exists: bool,
    schema_applied: bool,
    resume_initialization: bool = False,
    public_table_count: int | None = None,
) -> ProvisionAction:
    if not db_exists:
        return "create_and_apply"
    if schema_applied:
        return "noop"
    if not resume_initialization:
        raise RuntimeError(
            f"database {identity!r} has an incomplete schema without initialization authority"
        )
    if public_table_count != 0:
        raise RuntimeError("incomplete initialization contains unknown database objects")
    return "resume_apply"


def plan_pgvector_extension(
    *,
    extension_available: bool,
) -> str | None:
    if not extension_available:
        return None
    return "CREATE EXTENSION IF NOT EXISTS vector"
