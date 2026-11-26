from __future__ import annotations

import random
from pathlib import Path

from base.config import ConfigBoot
from gateway.http.config import gateway_config_from_boot
from services.derived.embeddinggemma.contract import DEFAULT_MODEL
from services.derived.memory_indexer.embeddings import embeddinggemma, factory

REVISION = "e7a8a2204b91e0f96e92960e84a09a89212e1633dcb7575a9bf3378b4df77f4c"
HOST = "10.64.0.7"
OTHER = "10.64.0.9"


def _config(tmp_path: Path, **fields: object) -> embeddinggemma.EmbeddingGemmaConfig:
    boot = ConfigBoot()
    boot.set_field("embedding_backend", "embeddinggemma")
    boot.set_field("embeddinggemma_revision", REVISION)
    for name, value in fields.items():
        boot.set_field(name, value)
    gateway = gateway_config_from_boot(
        boot,
        user_home=tmp_path,
        home=tmp_path,
        memory_root=tmp_path / "memory",
        rng=random.Random(0),  # noqa: S311
    )
    config = gateway.memory_search.embeddinggemma_config
    assert config is not None
    return config


def test_a_remote_gateway_dials_the_placement_address(tmp_path: Path) -> None:
    config = _config(
        tmp_path, embeddinggemma_host=HOST, embeddinggemma_port=9400, machine_host=OTHER
    )
    assert config.endpoint == f"http://{HOST}:9400/v1/embeddings"
    assert config.model == DEFAULT_MODEL


def test_the_gateway_on_the_hosting_machine_dials_loopback(tmp_path: Path) -> None:
    config = _config(tmp_path, embeddinggemma_host=HOST, machine_host=HOST)
    assert config.endpoint == "http://127.0.0.1:9379/v1/embeddings"


def test_an_explicit_endpoint_and_model_still_win(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        embeddinggemma_host=HOST,
        machine_host=OTHER,
        embeddinggemma_endpoint="http://127.0.0.1:9379/v1/embeddings",
        embeddinggemma_model="/home/zzy/litert-probe/model.litertlm,cpu",
    )
    assert config.endpoint == "http://127.0.0.1:9379/v1/embeddings"
    assert config.model == "/home/zzy/litert-probe/model.litertlm,cpu"


def test_the_space_fingerprint_is_the_revisions_whatever_the_device_or_host(
    tmp_path: Path,
) -> None:
    expected = factory.get_descriptor("embeddinggemma", revision=REVISION).fingerprint
    variants = (
        {
            "embeddinggemma_endpoint": "http://127.0.0.1:9379/v1/embeddings",
            "embeddinggemma_model": "/home/zzy/litert-probe/model.litertlm,cpu",
        },
        {
            "embeddinggemma_endpoint": "http://127.0.0.1:9379/v1/embeddings",
            "embeddinggemma_model": "/home/zzy/litert-probe/model.litertlm,gpu",
        },
        {"embeddinggemma_host": HOST, "machine_host": HOST, "embeddinggemma_device": "cpu"},
        {"embeddinggemma_host": HOST, "machine_host": HOST, "embeddinggemma_device": "gpu"},
        {"embeddinggemma_host": OTHER, "machine_host": HOST, "embeddinggemma_port": 9500},
    )
    for fields in variants:
        provider = embeddinggemma.EmbeddingGemmaProvider(_config(tmp_path, **fields), timeout_s=1)
        assert provider.fingerprint == expected, fields
