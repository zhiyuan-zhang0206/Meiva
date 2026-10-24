from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from base.config import ConfigBoot
from services.derived.memory_indexer.config import MemoryIndexerConfig, MemoryIndexerInputs


def test_tuning_defaults_are_the_production_values() -> None:
    tuning = MemoryIndexerConfig()
    assert (
        tuning.loop_interval_s,
        tuning.patrol_interval_s,
        tuning.retry_backoff_s,
        tuning.retry_limit,
        tuning.excluded_basenames,
        tuning.reconcile_chunk_paths,
        tuning.batch_size,
    ) == (1.0, 900.0, 60.0, 3, ("MEMORY.md", "index.md"), 64, 32)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("loop_interval_s", 0.0),
        ("loop_interval_s", -1.0),
        ("patrol_interval_s", 0.0),
        ("patrol_interval_s", -1.0),
        ("retry_backoff_s", 0.0),
        ("retry_backoff_s", -1.0),
        ("retry_limit", -1),
        ("reconcile_chunk_paths", 0),
        ("batch_size", 0),
        ("batch_size", -1),
    ],
)
def test_tuning_rejects_values_that_would_stall_or_skip_work(field: str, value: float) -> None:
    overrides: dict[str, Any] = {field: value}
    with pytest.raises(ValueError, match=field):
        MemoryIndexerConfig(**overrides)


def test_excluded_basenames_must_be_plain_file_names() -> None:
    for name in ("", "sub/index.md", "sub\\index.md"):
        with pytest.raises(ValueError, match="excluded_basenames"):
            MemoryIndexerConfig(excluded_basenames=(name,))


def test_inputs_are_read_once_from_their_own_owner() -> None:
    first, second = ConfigBoot(), ConfigBoot()
    for owner, name, timeout, key in (
        (first, "first", 11.0, "first-key"),
        (second, "second", 22.0, "second-key"),
    ):
        owner.set_field("embedding_backend", name)
        owner.set_field("memory_embed_timeout_seconds", timeout)
        owner.set_field("gemini_api_key", SecretStr(key))
        owner.set_field("memory_search_backend", name)
        owner.set_field("memory_search_uri", f"http://{name}")

    first_inputs = MemoryIndexerInputs.from_boot(first)
    second_inputs = MemoryIndexerInputs.from_boot(second)

    assert not first.prepared and not second.prepared
    assert first.upgrades == second.upgrades == 0
    assert first_inputs == MemoryIndexerInputs(
        embedding_name="first",
        embed_timeout_s=11.0,
        api_key="first-key",
        backend_name="first",
        search_uri="http://first",
    )
    assert second_inputs == MemoryIndexerInputs(
        embedding_name="second",
        embed_timeout_s=22.0,
        api_key="second-key",
        backend_name="second",
        search_uri="http://second",
    )


def test_inputs_are_restart_bound() -> None:
    owner = ConfigBoot()
    owner.set_field("memory_embed_timeout_seconds", 11.0)
    owner.set_field("gemini_api_key", SecretStr("old-key"))
    inputs = MemoryIndexerInputs.from_boot(owner)

    owner.set_field("memory_embed_timeout_seconds", 33.0)
    owner.set_field("gemini_api_key", None)

    assert inputs.embed_timeout_s == 11.0
    assert inputs.api_key == "old-key"


def test_a_missing_key_stays_missing_and_a_present_key_stays_out_of_the_repr() -> None:
    keyed, keyless = ConfigBoot(), ConfigBoot()
    keyed.set_field("gemini_api_key", SecretStr("secret-value"))
    keyless.set_field("gemini_api_key", None)

    assert MemoryIndexerInputs.from_boot(keyless).api_key is None
    assert "secret-value" not in repr(MemoryIndexerInputs.from_boot(keyed))
