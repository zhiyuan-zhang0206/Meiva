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
