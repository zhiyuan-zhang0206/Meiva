from __future__ import annotations

import pytest

from base.config import ConfigBoot
from services.derived.embeddinggemma.contract import DEFAULT_MODEL
from services.derived.memory_indexer.config import MemoryIndexerInputs
from services.derived.memory_indexer.embeddings import embeddinggemma, factory

REVISION = "e7a8a2204b91e0f96e92960e84a09a89212e1633dcb7575a9bf3378b4df77f4c"
HOST = "10.64.0.7"
OTHER = "10.64.0.9"


def _boot(**fields: object) -> ConfigBoot:
    boot = ConfigBoot()
    boot.set_field("embedding_backend", "embeddinggemma")
    boot.set_field("embeddinggemma_revision", REVISION)
    for name, value in fields.items():
        boot.set_field(name, value)
    return boot


def _indexer(boot: ConfigBoot) -> embeddinggemma.EmbeddingGemmaConfig:
    config = MemoryIndexerInputs.from_boot(boot).embeddinggemma_config
    assert config is not None
    return config


def _fingerprint(config: embeddinggemma.EmbeddingGemmaConfig) -> str:
    return embeddinggemma.EmbeddingGemmaProvider(config, timeout_s=1).fingerprint


def test_the_indexer_derives_the_endpoint_and_default_model_from_the_placement() -> None:
    boot = _boot(embeddinggemma_host=HOST, embeddinggemma_port=9400, machine_host=OTHER)
    config = _indexer(boot)
    assert config.endpoint == f"http://{HOST}:9400/v1/embeddings"
    assert config.model == DEFAULT_MODEL
    assert config.revision == REVISION


def test_the_hosting_machine_dials_loopback() -> None:
    boot = _boot(embeddinggemma_host=HOST, machine_host=HOST)
    config = _indexer(boot)
    assert config.endpoint == "http://127.0.0.1:9379/v1/embeddings"


def test_explicit_endpoint_and_model_override_the_placement() -> None:
    boot = _boot(
        embeddinggemma_host=HOST,
        machine_host=OTHER,
        embeddinggemma_endpoint="http://external.example:8080/v1/embeddings",
        embeddinggemma_model="/legacy/model.litertlm,gpu",
    )
    config = _indexer(boot)
    assert config.endpoint == "http://external.example:8080/v1/embeddings"
    assert config.model == "/legacy/model.litertlm,gpu"


def test_an_unmanaged_deployment_is_unchanged_and_still_needs_both_values() -> None:
    legacy = _boot(
        embeddinggemma_endpoint="http://127.0.0.1:9379/v1/embeddings",
        embeddinggemma_model="/home/zzy/litert-probe/model.litertlm,cpu",
    )
    config = _indexer(legacy)
    assert config.endpoint == "http://127.0.0.1:9379/v1/embeddings"
    assert config.model == "/home/zzy/litert-probe/model.litertlm,cpu"
    with pytest.raises(ValueError, match="MEIVA_EMBEDDINGGEMMA_HOST"):
        _indexer(_boot(embeddinggemma_endpoint="http://127.0.0.1:9379/v1/embeddings"))


def test_the_space_fingerprint_survives_every_device_and_placement_change() -> None:
    legacy_cpu = _boot(
        embeddinggemma_endpoint="http://127.0.0.1:9379/v1/embeddings",
        embeddinggemma_model="/home/zzy/litert-probe/model.litertlm,cpu",
    )
    legacy_gpu = _boot(
        embeddinggemma_endpoint="http://127.0.0.1:9379/v1/embeddings",
        embeddinggemma_model="/home/zzy/litert-probe/model.litertlm,gpu",
    )
    managed_cpu = _boot(embeddinggemma_host=HOST, machine_host=HOST, embeddinggemma_device="cpu")
    managed_gpu = _boot(embeddinggemma_host=HOST, machine_host=HOST, embeddinggemma_device="gpu")
    moved = _boot(embeddinggemma_host=OTHER, embeddinggemma_port=9500, machine_host=HOST)
    expected = factory.get_descriptor("embeddinggemma", revision=REVISION).fingerprint
    for boot in (legacy_cpu, legacy_gpu, managed_cpu, managed_gpu, moved):
        assert _fingerprint(_indexer(boot)) == expected


def test_a_different_revision_is_the_only_thing_that_changes_the_space() -> None:
    first = _fingerprint(_indexer(_boot(embeddinggemma_host=HOST)))
    other = _boot(embeddinggemma_host=HOST, embeddinggemma_revision="a" * 64)
    assert _fingerprint(_indexer(other)) != first
