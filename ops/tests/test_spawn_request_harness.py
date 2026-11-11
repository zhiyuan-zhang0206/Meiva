# meiva:note
# `SpawnAgentRequest` reads `config["harness"]` as the agent's runtime loop: validation takes it out
# of `config`, a dump puts it back (so a request round-trips and its creation identity covers it),
# and a bad value is a validation error rather than a default.

from __future__ import annotations

import pytest
from pydantic import ValidationError

from base.api_contracts.agent_contract import Harness
from ops.rpc_schemas import SpawnAgentRequest


def test_validation_takes_the_harness_out_of_config() -> None:
    request = SpawnAgentRequest(config={"harness": "codex", "llm_model": "gpt-6.1-terra"})
    assert request.harness is Harness.CODEX
    assert request.config == {"llm_model": "gpt-6.1-terra"}


def test_a_request_without_a_harness_has_none() -> None:
    request = SpawnAgentRequest(config={"llm_model": "deepseek-flash"})
    assert request.harness is None
    assert request.model_dump(mode="json")["config"] == {"llm_model": "deepseek-flash"}


def test_a_dump_round_trips_the_harness() -> None:
    request = SpawnAgentRequest(config={"harness": "codex", "llm_model": "gpt-6.1-terra"})
    dumped = request.model_dump(mode="json")
    assert dumped["config"] == {"llm_model": "gpt-6.1-terra", "harness": "codex"}
    again = SpawnAgentRequest.model_validate(dumped)
    assert again.harness is Harness.CODEX
    assert again.model_dump(mode="json") == dumped


def test_a_fork_may_name_only_a_harness() -> None:
    request = SpawnAgentRequest(fork_from=7, config={"harness": "meiva"})
    assert request.harness is Harness.MEIVA
    assert request.config is None


def test_the_old_top_level_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="harness"):
        SpawnAgentRequest.model_validate(
            {"config": {"llm_model": "m"}, "harness": "codex"},
        )


@pytest.mark.parametrize("bad", ["claude-code", None, 3])
def test_a_bad_harness_is_a_validation_error(bad: object) -> None:
    with pytest.raises(ValidationError, match="overlay key 'harness'"):
        SpawnAgentRequest(config={"harness": bad, "llm_model": "m"})
