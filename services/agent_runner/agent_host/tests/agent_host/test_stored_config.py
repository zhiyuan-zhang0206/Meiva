from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

from base.host.proc import run_bounded
from services.agent_runner.agent_host.scheduling.stored_config import (
    StoredConfig,
    config_fingerprint,
)

_CHILD = Path(__file__).with_name("stored_config_import_child.py")


def test_the_fingerprint_ignores_key_order() -> None:
    a = config_fingerprint({"x": 1, "y": 2}, None)
    b = config_fingerprint({"y": 2, "x": 1}, None)
    assert a == b
    assert a != config_fingerprint({"x": 1, "y": 3}, None)


def test_the_fingerprint_separates_the_two_maps() -> None:
    assert config_fingerprint({"x": 1}, None) != config_fingerprint(None, {"x": 1})


def test_absent_and_empty_stored_maps_have_distinct_fingerprints() -> None:
    assert config_fingerprint(None, None) != config_fingerprint({}, None)
    assert config_fingerprint(None, None) != config_fingerprint(None, {})


def test_non_json_decoded_scalars_keep_string_coercion() -> None:
    assert config_fingerprint({"x": Decimal("1.25")}, None) == config_fingerprint(
        {"x": "1.25"}, None
    )


def test_the_row_retains_its_maps_and_observes_nested_edits() -> None:
    nested = {"enabled": True}
    overlay: dict[str, object] = {"feature": nested}
    birth: dict[str, object] = {"llm_model": "initial-model"}
    stored = StoredConfig("runner", "idling", "meiva", overlay, birth)
    before = stored.fingerprint
    assert stored.config_overlay is overlay
    assert stored.birth_config is birth
    nested["enabled"] = False
    assert stored.fingerprint != before


def test_wake_screening_does_not_initialize_runtime_construction() -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_CHILD)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["same_identity"] is True
    assert observed["loaded_observers"] == []
