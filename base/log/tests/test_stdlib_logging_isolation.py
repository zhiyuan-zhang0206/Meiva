from __future__ import annotations

import logging

import pytest

from base.log import FIRST_PARTY_LOGGER_NAMES, StdlibInterceptHandler, install_stdlib_intercept
from base.log.tests.stdlib_logging import stdlib_logging_isolated


def _all_levels() -> dict[str, int]:
    levels = {"": logging.getLogger().level}
    for name, logger in logging.Logger.manager.loggerDict.items():
        if isinstance(logger, logging.Logger):
            levels[name] = logger.level
    return levels


def test_the_guard_is_wired_onto_every_test(request: pytest.FixtureRequest) -> None:
    assert "_no_stdlib_telemetry_bridge" in request.fixturenames


def test_a_real_intercept_install_is_undone_level_for_level() -> None:
    before = _all_levels()
    handlers_before = logging.getLogger().handlers[:]

    with stdlib_logging_isolated():
        install_stdlib_intercept()
        assert logging.getLogger().level == logging.INFO
        assert all(logging.getLogger(n).level == logging.DEBUG for n in FIRST_PARTY_LOGGER_NAMES)
        assert any(isinstance(h, StdlibInterceptHandler) for h in logging.getLogger().handlers)

    after = _all_levels()
    assert {name: level for name, level in after.items() if name in before} == before, (
        "a logger `install_stdlib_intercept` sets is missing from "
        "`_INTERCEPT_LEVELED_THIRD_PARTY` in tests/fixtures/guards.py"
    )
    assert logging.getLogger().handlers == handlers_before


def test_a_first_party_logger_is_back_at_its_original_level_after_the_block() -> None:
    probe = logging.getLogger("services.stdlib_logging_isolation_probe")
    original = probe.getEffectiveLevel()
    with stdlib_logging_isolated():
        install_stdlib_intercept()
        assert probe.getEffectiveLevel() == logging.DEBUG
    assert probe.getEffectiveLevel() == original


def test_the_block_restores_on_an_exception() -> None:
    before = _all_levels()
    root = logging.getLogger()
    handlers_before = root.handlers[:]
    with pytest.raises(RuntimeError, match="boom"), stdlib_logging_isolated():
        install_stdlib_intercept()
        root.handlers = []
        raise RuntimeError("boom")
    assert {n: lv for n, lv in _all_levels().items() if n in before} == before
    assert root.handlers == handlers_before


def test_the_bridge_handler_is_off_the_root_inside_the_block() -> None:
    root = logging.getLogger()
    bridge = StdlibInterceptHandler()
    root.addHandler(bridge)
    try:
        with stdlib_logging_isolated():
            assert bridge not in root.handlers
        assert bridge in root.handlers
    finally:
        root.removeHandler(bridge)


@pytest.mark.parametrize("ancestor", ["", "stdlib_isolation.level_parent"])
def test_restoring_an_ancestor_invalidates_cached_child_levels(ancestor: str) -> None:
    parent = logging.getLogger(ancestor)
    child = parent.getChild("cached_child")
    parent.setLevel(logging.ERROR)
    child.setLevel(logging.NOTSET)
    assert not child.isEnabledFor(logging.DEBUG)

    with stdlib_logging_isolated():
        parent.setLevel(logging.DEBUG)
        assert child.isEnabledFor(logging.DEBUG)

    assert child.getEffectiveLevel() == logging.ERROR
    assert not child.isEnabledFor(logging.DEBUG)


def test_a_new_logger_returns_to_inherited_levels_and_invalidates_its_children() -> None:
    root = logging.getLogger()
    root.setLevel(logging.ERROR)
    with stdlib_logging_isolated():
        parent = logging.getLogger("stdlib_isolation.new_parent")
        child = parent.getChild("child")
        parent.setLevel(logging.DEBUG)
        assert child.isEnabledFor(logging.DEBUG)

    assert parent.level == logging.NOTSET
    assert child.level == logging.NOTSET
    assert child.getEffectiveLevel() == logging.ERROR
    assert not child.isEnabledFor(logging.DEBUG)


def test_nested_blocks_restore_each_outer_level() -> None:
    probe = logging.getLogger("stdlib_isolation.nested")
    probe.setLevel(logging.ERROR)
    with stdlib_logging_isolated():
        probe.setLevel(logging.INFO)
        with stdlib_logging_isolated():
            probe.setLevel(logging.DEBUG)
            assert probe.isEnabledFor(logging.DEBUG)
        assert probe.level == logging.INFO
        assert not probe.isEnabledFor(logging.DEBUG)
    assert probe.level == logging.ERROR
