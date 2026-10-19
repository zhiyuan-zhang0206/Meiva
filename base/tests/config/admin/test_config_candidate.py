"""Regression coverage for full-candidate validation before config persistence.

The cross-field invariant these tests lean on is the sandbox one: the inner
`exec_timeout_seconds` must stay below the outer `exec_node_timeout_seconds`.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

from base.config import runtime_config
from base.config.admin.candidate import EnvPatchValidation

type EnvPatchValidator = Callable[[dict[str, object], set[str]], list[str]]


@pytest.fixture
def configured_env(tmp_path: Path) -> Path:
    """A `.env` — outside this unit's home — carrying a valid sandbox timeout pair."""
    env_path = tmp_path / ".env"
    runtime_config.write_fields(
        {"exec_timeout_seconds": 300, "exec_node_timeout_seconds": 1200}, set(), path=env_path
    )
    return env_path


def _write(
    env_path: Path, updates: dict[str, object], *, expected_digest: str | None = None
) -> None:
    runtime_config.write_fields(updates, set(), path=env_path, expected_digest=expected_digest)


def _validate_env_patch(env_path: Path) -> EnvPatchValidator:
    from base.config.admin.candidate import validate_env_patch

    return partial(validate_env_patch, path=env_path)


def _validate_env_patch_for_write(
    env_path: Path,
) -> Callable[[dict[str, object], set[str]], EnvPatchValidation]:
    from base.config.admin.candidate import validate_env_patch_for_write

    return partial(validate_env_patch_for_write, path=env_path)


def test_patch_that_breaks_a_cross_field_invariant_is_rejected(configured_env: Path) -> None:
    """The persisted candidate must satisfy the validators that run at startup."""
    errors = _validate_env_patch(configured_env)({"exec_node_timeout_seconds": 200}, set())

    assert errors
    assert any("exec_node_timeout_seconds" in error for error in errors)


def test_patch_that_keeps_the_invariant_is_valid(configured_env: Path) -> None:
    assert _validate_env_patch(configured_env)({"exec_node_timeout_seconds": 900}, set()) == []


def test_unrelated_domain_patch_is_valid(configured_env: Path) -> None:
    """Validation reconstructs only the domain the patch changes."""
    assert _validate_env_patch(configured_env)({"llm_model": "candidate-model"}, set()) == []


def test_unrelated_invalid_domain_does_not_reject_a_candidate(configured_env: Path) -> None:
    """A broken sandbox file value cannot poison an independent model candidate."""
    _write(configured_env, {"exec_timeout_seconds": 1200, "exec_node_timeout_seconds": 300})

    assert _validate_env_patch(configured_env)({"llm_model": "candidate-model"}, set()) == []


def test_removal_uses_the_field_default(configured_env: Path) -> None:
    """Removing a field falls back to its default, which the candidate is checked with."""
    _write(configured_env, {"exec_node_timeout_seconds": 900})

    assert _validate_env_patch(configured_env)({}, {"exec_node_timeout_seconds"}) == []


def test_removal_that_leaves_an_invalid_pair_is_rejected(configured_env: Path) -> None:
    """The default stands in for a removed field, so removal can still break the invariant."""
    _write(configured_env, {"exec_timeout_seconds": 1500, "exec_node_timeout_seconds": 2000})

    errors = _validate_env_patch(configured_env)({}, {"exec_node_timeout_seconds"})

    assert any("exec_node_timeout_seconds" in error for error in errors)


def test_removing_required_field_is_invalid(configured_env: Path) -> None:
    """A required data-plane value cannot disappear from the persisted candidate."""
    errors = _validate_env_patch(configured_env)({}, {"db_url"})

    assert errors
    assert any("MEIVA_DB_URL" in error and "Field required" in error for error in errors)


def test_validate_or_raise_joins_candidate_errors(configured_env: Path) -> None:
    """Non-HTTP callers receive the same alias-safe candidate explanation."""
    from base.config.admin.candidate import validate_env_patch_or_raise

    with pytest.raises(ValueError, match="exec_node_timeout_seconds"):
        validate_env_patch_or_raise({"exec_node_timeout_seconds": 200}, set(), path=configured_env)


def test_current_invalid_domain_is_rejected_until_the_patch_repairs_it(
    configured_env: Path,
) -> None:
    """A same-domain edit cannot hide a pre-existing invalid timeout pair."""
    _write(configured_env, {"exec_node_timeout_seconds": 200})
    validate_env_patch = _validate_env_patch(configured_env)

    errors = validate_env_patch({"exec_output_max_chars": 40000}, set())

    assert any("exec_node_timeout_seconds" in error for error in errors)
    assert validate_env_patch({"exec_node_timeout_seconds": 900}, set()) == []


def test_empty_patch_is_trivially_valid(configured_env: Path) -> None:
    """No touched domain means no candidate reconstruction or rejection."""
    assert _validate_env_patch(configured_env)({}, set()) == []


def test_stale_candidate_digest_cannot_persist_an_invalid_combination(
    configured_env: Path,
) -> None:
    """A later valid patch must retry after another candidate has changed `.env`."""
    validate_for_write = _validate_env_patch_for_write(configured_env)
    raise_inner = validate_for_write({"exec_timeout_seconds": 1000}, set())
    lower_outer = validate_for_write({"exec_node_timeout_seconds": 900}, set())

    assert raise_inner.errors == []
    assert lower_outer.errors == []
    assert raise_inner.expected_digest == lower_outer.expected_digest

    _write(
        configured_env, {"exec_timeout_seconds": 1000}, expected_digest=raise_inner.expected_digest
    )
    with pytest.raises(RuntimeError, match="changed before owned runtime-config write"):
        _write(
            configured_env,
            {"exec_node_timeout_seconds": 900},
            expected_digest=lower_outer.expected_digest,
        )
