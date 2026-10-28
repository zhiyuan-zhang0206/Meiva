# meiva:note
# `meizel/state/turn_scoped_config_plan.py` unit tests: which files the rule skips, and which lines
# read a per-agent field through the bare settings singleton, through the model layering by name or
# through the process-global plugin map. Asserted by value; the rule end to end against a temporary
# tree is `test_lint_turn_scoped_config.py`.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

import pytest

from meizel.state import turn_scoped_config_plan as plan

_EXEMPT = frozenset({"base/config/pins.py"})


@pytest.mark.parametrize(
    ("rel", "name", "skipped"),
    [
        ("base/config/pins.py", "pins.py", True),
        ("base/config/tests/mod.py", "mod.py", True),
        ("tests/mod.py", "mod.py", True),
        ("base/config/test_mod.py", "test_mod.py", True),
        ("base/config/mod_test.py", "mod_test.py", True),
        ("base/config/mod.py", "mod.py", False),
        ("base/config/testing/mod.py", "mod.py", False),
    ],
)
def test_the_rule_skips_exempt_paths_tests_directories_and_test_modules(
    rel: str, name: str, skipped: bool
) -> None:
    assert plan.skips(rel, name, allowed_files=_EXEMPT) is skipped


def test_a_test_module_is_skipped_by_the_name_the_scan_reached() -> None:
    assert plan.skips("base/config/linked.py", "test_mod.py", allowed_files=_EXEMPT) is True


def test_a_per_agent_field_read_by_name_without_overrides_is_a_finding() -> None:
    text = 'model = resolve_setting("model", model=model)\n'
    assert plan.layered_reads(text, "base/config/a.py", {"model"}) == [
        'base/config/a.py:1: resolve_setting("model", model=model)'
    ]


def test_the_same_read_with_the_agents_overrides_passes() -> None:
    text = 'model = resolve_setting("model", model=model, overrides=agent.overrides)\n'
    assert plan.layered_reads(text, "base/config/a.py", {"model"}) == []


def test_a_field_that_is_not_per_agent_is_not_a_finding() -> None:
    assert plan.layered_reads('get_field("db_url")\n', "base/config/a.py", {"model"}) == []


def test_a_per_agent_field_read_through_the_singleton_is_a_finding() -> None:
    text = "value = settings.agent.model\n"
    assert plan.settings_reads(text, "base/config/a.py", {"model"}) == [
        "base/config/a.py:1: value = settings.agent.model"
    ]


def test_a_singleton_read_of_a_cluster_field_or_a_comment_is_not_a_finding() -> None:
    text = "# value = settings.agent.model\nvalue = settings.db.url\n"
    assert plan.settings_reads(text, "base/config/a.py", {"model"}) == []


def test_reading_the_plugin_map_by_subscript_is_a_finding() -> None:
    text = 'config = _PLUGIN_CONFIGS["search"]\n'
    assert plan.plugin_config_reads(text, "base/config/a.py", frozenset()) == [
        'base/config/a.py:1: config = _PLUGIN_CONFIGS["search"]'
    ]


def test_the_plugin_mechanism_itself_and_a_registration_probe_are_not_findings() -> None:
    text = 'config = _PLUGIN_CONFIGS["search"]\n'
    mechanism = frozenset({"base/config/a.py"})
    assert plan.plugin_config_reads(text, "base/config/a.py", mechanism) == []
    assert plan.plugin_config_reads('if "x" in _PLUGIN_CONFIGS:\n', "b/c.py", frozenset()) == []


def test_every_heading_says_what_the_read_should_have_been() -> None:
    assert "get_plugin_config" in plan.PLUGIN_HEADING
    assert "overrides" in plan.LAYERED_HEADING
    assert "agent_setting" in plan.SINGLETON_HEADING
