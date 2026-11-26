from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from services.derived.embeddinggemma import engine as engine_module
from services.derived.embeddinggemma.contract import DIM
from services.derived.embeddinggemma.engine import EmbeddingRejectedError, open_litert_engine
from services.derived.embeddinggemma.tests.fakes import FakeRaw, FakeRuntime, server_config


@pytest.mark.parametrize(("device", "backend"), [("cpu", "CPU"), ("gpu", "GPU")])
def test_the_device_alone_selects_the_backend(tmp_path: Path, device: str, backend: str) -> None:
    fake = FakeRuntime()
    config = server_config(tmp_path, device=device)
    opened = open_litert_engine(config, runtime=fake.runtime())
    assert fake.opened_with == [(config.model_path, backend, config.cache_dir, 8192)]
    assert opened.signatures == (7,)
    assert Path(config.cache_dir).is_dir()


def test_the_warmup_embeds_one_text_before_the_engine_is_returned(tmp_path: Path) -> None:
    fake = FakeRuntime()
    open_litert_engine(server_config(tmp_path), runtime=fake.runtime())
    assert fake.raw.options_seen == [("options", True, True, DIM)]


def test_a_hash_mismatch_fails_before_the_runtime_loads_the_file(tmp_path: Path) -> None:
    fake = FakeRuntime()
    config = server_config(tmp_path, revision="0" * 64)
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        open_litert_engine(config, runtime=fake.runtime())
    assert fake.opened_with == []


def test_a_missing_model_file_fails_fast(tmp_path: Path) -> None:
    config = server_config(tmp_path, model_path=str(tmp_path / "absent.litertlm"))
    with pytest.raises(FileNotFoundError):
        open_litert_engine(config, runtime=FakeRuntime().runtime())


def test_a_failed_warmup_closes_the_engine_and_propagates(tmp_path: Path) -> None:
    fake = FakeRuntime(raw=FakeRaw(fail_with=ValueError("device lost")))
    with pytest.raises(EmbeddingRejectedError, match="device lost"):
        open_litert_engine(server_config(tmp_path), runtime=fake.runtime())
    assert fake.raw.closed == 1


def test_compute_failures_map_to_a_rejection_and_close_reaches_the_engine(tmp_path: Path) -> None:
    fake = FakeRuntime()
    opened = open_litert_engine(server_config(tmp_path), runtime=fake.runtime())
    fake.raw.fail_with = RuntimeError("input is longer than max_input_length")
    with pytest.raises(EmbeddingRejectedError, match="longer than"):
        opened.engine.embed(["x"], normalize=True, insert_special_tokens=True, dimensions=DIM)
    opened.engine.close()
    assert fake.raw.closed == 1


def test_the_request_options_reach_the_runtime(tmp_path: Path) -> None:
    fake = FakeRuntime()
    opened = open_litert_engine(server_config(tmp_path), runtime=fake.runtime())
    vectors = opened.engine.embed(
        ["ab", "abc"], normalize=False, insert_special_tokens=False, dimensions=DIM
    )
    assert fake.raw.options_seen[-1] == ("options", False, False, DIM)
    assert [row[0] for row in vectors] == [2.0, 3.0]


def test_a_missing_runtime_names_both_ways_to_supply_it() -> None:
    def absent(name: str) -> ModuleType:
        raise ImportError(f"No module named {name!r}")

    with pytest.raises(RuntimeError, match="MEIVA_EMBEDDINGGEMMA_PYTHON") as raised:
        engine_module.load_runtime(None, import_module=absent)
    assert "MEIVA_EMBEDDINGGEMMA_LITERT_PATH" in str(raised.value)


def test_the_litert_path_is_appended_after_the_environments_own_packages() -> None:
    marker = "/opt/embeddinggemma-litert-test"

    def absent(name: str) -> ModuleType:
        raise ImportError(name)

    try:
        with pytest.raises(RuntimeError):
            engine_module.load_runtime(marker, import_module=absent)
        assert engine_module.sys.path[-1] == marker
    finally:
        engine_module.sys.path.remove(marker)
