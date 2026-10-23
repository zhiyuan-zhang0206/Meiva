# meiva:note
# The fork config rule, by value: what a fork may differ in from its source.

from __future__ import annotations

from base.api_contracts.agent_contract import ForkConfigChangeNotAllowed
from gateway.agents.launch.fork_plan import FORK_SKILL_KEYS, ForkConfigPlan, fork_config_plan

INJECT, EXPAND = FORK_SKILL_KEYS


def test_a_fork_without_config_inherits_the_source_overlay_verbatim() -> None:
    plan = fork_config_plan({"llm_model": "m"}, {}, None)
    assert plan == ForkConfigPlan({"llm_model": "m"}, None)


def test_a_fork_of_a_source_without_overlay_stores_none() -> None:
    assert fork_config_plan({}, {"llm_model": "birth"}, None) == ForkConfigPlan(None, None)


def test_repeating_the_source_values_is_not_a_change() -> None:
    plan = fork_config_plan({"llm_model": "m"}, {}, {"llm_model": "m"})
    assert plan == ForkConfigPlan({"llm_model": "m"}, None)


def test_changing_a_key_that_is_not_a_skill_list_is_refused() -> None:
    plan = fork_config_plan({"llm_model": "m"}, {}, {"llm_model": "other"})
    assert isinstance(plan, ForkConfigChangeNotAllowed)
    assert "['llm_model']" in str(plan)


def test_adding_a_skill_is_allowed_and_loaded_at_the_context_tail() -> None:
    plan = fork_config_plan({INJECT: ["a"]}, {}, {INJECT: ["a", "b"]})
    assert plan == ForkConfigPlan({INJECT: ["a", "b"]}, ["b"])


def test_a_skill_also_expanded_at_start_is_not_a_tail_addition() -> None:
    plan = fork_config_plan({INJECT: ["a"], EXPAND: []}, {}, {INJECT: ["a", "b"], EXPAND: ["b"]})
    assert isinstance(plan, ForkConfigPlan)
    assert plan.tail_skills is None


def test_removing_a_skill_is_refused() -> None:
    plan = fork_config_plan({INJECT: ["a", "b"]}, {}, {INJECT: ["a"]})
    assert isinstance(plan, ForkConfigChangeNotAllowed)
    assert INJECT in str(plan)


def test_the_birth_config_counts_as_the_source_s_effective_skills() -> None:
    plan = fork_config_plan({}, {INJECT: ["from-birth"]}, {INJECT: ["from-birth", "new"]})
    assert plan == ForkConfigPlan({INJECT: ["from-birth", "new"]}, ["new"])


def test_a_skill_value_that_is_not_a_list_of_strings_cannot_be_extended() -> None:
    plan = fork_config_plan({INJECT: "a"}, {}, {INJECT: ["a", "b"]})
    assert isinstance(plan, ForkConfigChangeNotAllowed)
