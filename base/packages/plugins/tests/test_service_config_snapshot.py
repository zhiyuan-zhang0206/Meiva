from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV
from base.packages.plugins.config_registration import (
    read_config_image,
    service_config_packet,
)
from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError
from base.packages.plugins.tests.plugin_boundary import run_boundary


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool = Field(default=True, strict=True)
    interval: float = Field(default=300, strict=True)


def test_service_birth_snapshot_survives_authority_image_changes(
    tmp_path: Path,
) -> None:
    image = tmp_path / "config.json"
    image.write_text('{"enabled":true,"interval":42}')
    captured = read_config_image(Config, image)
    packet = service_config_packet("fleet", captured)
    image.write_text('{"enabled":false,"interval":900}')
    run_boundary(
        "service-snapshot",
        home=tmp_path,
        env={SERVICE_PLUGIN_CONFIG_ENV: packet},
        args=[str(image)],
    )


def test_absent_service_carrier_is_distinct_from_invalid(tmp_path: Path) -> None:
    run_boundary("service-carrier", home=tmp_path, env={}, args=["absent"])


@pytest.mark.parametrize(
    ("packet", "error"),
    [
        ("bad json", InvalidConfigData),
        ('{"plugin":"fleet","config":{},"unknown":1}', InvalidConfigData),
        ('{"plugin":"other","config":{}}', InvalidConfigData),
        ('{"plugin":"fleet","config":{"enabled":true}}', SchemaDriftError),
        ('{"plugin":"fleet","config":{"enabled":"true","interval":300}}', InvalidConfigData),
    ],
)
def test_present_service_packet_never_falls_back(
    packet: str,
    error: type[Exception],
    tmp_path: Path,
) -> None:
    run_boundary(
        "service-carrier",
        home=tmp_path,
        env={SERVICE_PLUGIN_CONFIG_ENV: packet},
        args=[error.__name__],
    )


def test_canonical_packet_rejects_sensitive_fields_and_nonfinite_values() -> None:
    class SecretConfig(BaseModel):
        token: str = Field(default="secret", json_schema_extra={"sensitive": True})

    with pytest.raises(InvalidConfigData, match="sensitive"):
        service_config_packet("fleet", SecretConfig())
    with pytest.raises(ValueError):
        service_config_packet("fleet", Config(interval=float("nan")))
    assert service_config_packet("fleet", Config()) == service_config_packet("fleet", Config())
