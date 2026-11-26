from __future__ import annotations

import pytest
from pydantic import ValidationError

from base.config import Settings

SERVICES = type(Settings().services)


def test_a_default_deployment_manages_nothing() -> None:
    settings = SERVICES()
    assert settings.embeddinggemma_host == ""
    assert settings.embeddinggemma_endpoint is None
    assert settings.embeddinggemma_model is None
    assert settings.embeddinggemma_device == "cpu"
    assert settings.embeddinggemma_port == 9379


def test_the_model_and_cache_default_under_the_unit_home() -> None:
    settings = SERVICES()
    assert settings.embeddinggemma_model_path.name == "model.litertlm"
    assert settings.embeddinggemma_model_path.parent.name == "embeddinggemma"
    assert settings.embeddinggemma_cache_dir.parent == settings.embeddinggemma_model_path.parent


@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_the_device_accepts_exactly_cpu_and_gpu(device: str) -> None:
    accepted = SERVICES.model_validate({"MEIVA_EMBEDDINGGEMMA_DEVICE": device})
    assert accepted.embeddinggemma_device == device


@pytest.mark.parametrize("device", ["cuda", "CPU", "", "cpu,gpu"])
def test_an_unknown_device_fails_fast(device: str) -> None:
    with pytest.raises(ValidationError, match="'cpu' or 'gpu'"):
        _ = SERVICES.model_validate({"MEIVA_EMBEDDINGGEMMA_DEVICE": device})


def test_placement_is_cluster_wide_and_everything_else_belongs_to_the_hosting_machine() -> None:
    fields = SERVICES.model_fields
    scopes: dict[str, object] = {}
    for name, field in fields.items():
        extra = field.json_schema_extra
        if name.startswith("embeddinggemma_") and isinstance(extra, dict):
            scopes[name] = extra["scope"]
    assert scopes["embeddinggemma_host"] == "cluster-pinned"
    assert scopes["embeddinggemma_port"] == "cluster-pinned"
    for name in (
        "embeddinggemma_device",
        "embeddinggemma_model_path",
        "embeddinggemma_python",
        "embeddinggemma_litert_path",
        "embeddinggemma_cache_dir",
        "embeddinggemma_bind_host",
        "embeddinggemma_max_input_length",
    ):
        assert scopes[name] == "host", name
