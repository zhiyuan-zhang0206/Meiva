# meiva:note
# The overlay keys that are columns of the agent's row (`harness`), read by one function: a
# missing key is None, a present key must name a `Harness` (an unknown value, a non-string or a null
# is refused, never defaulted), the config fields come back without it, and the config-field
# validators reject the key as unknown if it reaches them.

from __future__ import annotations

from pathlib import Path

import pytest

from base.agents.model.overlay_columns import COLUMN_OVERLAY_KEYS, split_overlay_columns
from base.api_contracts.agent_contract import Harness
from base.packages.plugins.config_registration import resolve_overlay_targets
from base.packages.plugins.plugin_errors import InvalidConfigOverlay


def test_a_missing_key_is_none_and_the_fields_are_untouched() -> None:
    assert split_overlay_columns(None) == (None, {})
    assert split_overlay_columns({"llm_model": "m"}) == (None, {"llm_model": "m"})


@pytest.mark.parametrize("harness", list(Harness))
def test_every_harness_name_is_taken_out_of_the_fields(harness: Harness) -> None:
    columns = split_overlay_columns({"harness": harness.value, "llm_model": "m"})
    assert columns.harness is harness
    assert columns.fields == {"llm_model": "m"}


@pytest.mark.parametrize("bad", ["claude-code", "", "CODEX", None, 3, ["codex"]])
def test_a_value_that_is_not_a_harness_is_refused(bad: object) -> None:
    with pytest.raises(InvalidConfigOverlay, match="overlay key 'harness'"):
        split_overlay_columns({"harness": bad})


def test_the_split_does_not_mutate_its_input() -> None:
    overlay: dict[str, object] = {"harness": "codex", "llm_model": "m"}
    split_overlay_columns(overlay)
    assert overlay == {"harness": "codex", "llm_model": "m"}


def test_the_config_field_validators_reject_a_key_that_was_not_taken_out(tmp_path: Path) -> None:
    assert {"harness"} == COLUMN_OVERLAY_KEYS
    with pytest.raises(InvalidConfigOverlay, match="overlay key 'harness' is not in"):
        resolve_overlay_targets({"harness": "codex"}, home=tmp_path)
