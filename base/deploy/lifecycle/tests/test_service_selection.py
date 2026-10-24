import pytest

from base.deploy.lifecycle import service_selection as selection


def test_each_explicit_form_resolves_its_skip_set() -> None:
    available = {"gateway", "ops", "new-plugin"}
    assert selection.resolve_selection(
        available, only=("gateway",), excluded=(), all_services=False
    ) == {"ops", "new-plugin"}
    assert selection.resolve_selection(
        available, only=(), excluded=("ops",), all_services=False
    ) == {"ops"}
    assert selection.resolve_selection(available, only=(), excluded=(), all_services=True) == set()


def test_a_start_names_exactly_one_selection() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        selection.resolve_selection({"gateway"}, only=(), excluded=(), all_services=False)
    with pytest.raises(ValueError, match="exactly one"):
        selection.resolve_selection({"gateway"}, only=("gateway",), excluded=(), all_services=True)


def test_unknown_names_refuse() -> None:
    with pytest.raises(ValueError, match="unknown"):
        selection.resolve_selection({"gateway"}, only=("typo",), excluded=(), all_services=False)


def test_stopping_extras_refuses_every_kept_unit_and_names_each_flag() -> None:
    refusal = selection.extras_keep_refusal(
        stop_extras=True, keep_shell_sessions=True, keep_browser=False, keep_service=True
    )

    assert refusal is not None
    assert "conflicting: --keep-shell-sessions, --keep-service" in refusal


def test_extras_stay_stoppable_when_nothing_is_kept_or_they_are_kept_too() -> None:
    assert (
        selection.extras_keep_refusal(
            stop_extras=True, keep_shell_sessions=False, keep_browser=False, keep_service=False
        )
        is None
    )
    assert (
        selection.extras_keep_refusal(
            stop_extras=False, keep_shell_sessions=True, keep_browser=True, keep_service=True
        )
        is None
    )
