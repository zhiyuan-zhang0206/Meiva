from pathlib import Path
from typing import Literal, Self

import pytest
from httpx2 import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugins.config_face import disk_image_path
from base.packages.plugins.extensions import PluginContributions
from base.paths import meiva_home
from gateway.tests.auth.gateway_config import gateway_test_client
from ops.host_config import config_write_op
from ops.rpc_schemas import ConfigWriteOpResult


class ValidatedFleetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_escalate_n: int = Field(
        default=3,
        description="Cluster-pinned policy threshold.",
        json_schema_extra={
            "env_var": "MEIVA_POLICY_ESCALATE_N",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )
    policy_maintenance_limit: int = Field(
        default=3,
        description="Host-scoped remotely writable policy limit.",
        json_schema_extra={
            "env_var": "MEIVA_POLICY_MAINTENANCE_LIMIT",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "capability": "gateway",
            "scope": "host",
            "remote_writable": True,
        },
    )

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.policy_escalate_n < 1 or self.policy_maintenance_limit < 1:
            raise ValueError("candidate Fleet policy rejected")
        if self.policy_escalate_n == 7 or self.policy_maintenance_limit == 7:
            image = disk_image_path("validated_policy", home=meiva_home())
            competing = image.with_name("competing.json")
            if competing.exists():
                image.write_bytes(competing.read_bytes())
                competing.unlink()
        return self


def contribute() -> PluginContributions:
    return PluginContributions(config=ValidatedFleetConfig)


@pytest.fixture
def owner_names() -> tuple[str, ...]:
    return ("validated_policy",)


@pytest.fixture
def owned_image(unit_home: Path, owner_names: tuple[str, ...]) -> Path:
    for name in owner_names:
        plugin_dir = unit_home / "plugins" / name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "meiva-plugin.json").write_text(
            f'{{"apiVersion": 2, "name": "{name}", "version": "1.0.0", "engines": {{"meiva": ">=0.1.0"}}}}\n',
            encoding="utf-8",
        )
        (plugin_dir / "plugin.py").write_text(
            "from base.packages.plugins.extensions import PluginContributions\n"
            "def contribute() -> PluginContributions:\n"
            "    return PluginContributions()\n",
            encoding="utf-8",
        )
        (plugin_dir / "default_config.py").write_text(
            "from gateway.tests.routers.config_api.test_plugin_write_errors import contribute\n",
            encoding="utf-8",
        )
    image = disk_image_path(owner_names[0], home=unit_home)
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_text(ValidatedFleetConfig().model_dump_json(), encoding="utf-8")
    return image


def write_request(
    consumer: Literal["gateway", "host"],
    *,
    bad_candidate: bool = False,
    config_authority: ConfigAuthority,
    local_machine: str,
) -> Response | ConfigWriteOpResult:
    if consumer == "gateway":
        with gateway_test_client() as client:
            return client.put("/api/config", json={"policy_escalate_n": -1 if bad_candidate else 7})
    return config_write_op(
        {"policy_maintenance_limit": -1 if bad_candidate else 7},
        local=False,
        authority=config_authority,
        local_machine=local_machine,
        host=fake_host(),
        home=meiva_home(),
    )


@pytest.mark.parametrize("consumer", ["gateway", "host"])
def test_invalid_whole_candidate_is_rejected_without_writing(
    owned_image: Path,
    consumer: Literal["gateway", "host"],
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    saved = owned_image.read_bytes()
    result = write_request(
        consumer,
        bad_candidate=True,
        config_authority=config_authority,
        local_machine=machine_identity.name(),
    )
    if isinstance(result, Response):
        assert result.status_code == 400
        assert "candidate Fleet policy rejected" in result.json()["detail"]
    else:
        assert result.applied is False
        assert all(not field.ok for field in result.results.values())
        assert "candidate Fleet policy rejected" in str(result.results)
        assert result.restart_required == []
    assert owned_image.read_bytes() == saved


@pytest.mark.parametrize("consumer", ["gateway", "host"])
def test_concurrent_image_change_is_rejected_without_overwriting(
    owned_image: Path,
    consumer: Literal["gateway", "host"],
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    concurrent = ValidatedFleetConfig(policy_escalate_n=19).model_dump_json().encode()
    owned_image.with_name("competing.json").write_bytes(concurrent)
    result = write_request(
        consumer, config_authority=config_authority, local_machine=machine_identity.name()
    )
    if isinstance(result, Response):
        assert result.status_code == 409
        assert "plugin config changed" in result.json()["detail"]
    else:
        assert result.applied is False
        assert all(not field.ok for field in result.results.values())
        assert "plugin config changed" in str(result.results)
        assert result.restart_required == []
    assert owned_image.read_bytes() == concurrent


@pytest.mark.parametrize("consumer", ["gateway", "host"])
def test_unexpected_persistence_failure_keeps_original_exception_and_bytes(
    owned_image: Path,
    consumer: Literal["gateway", "host"],
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    saved = owned_image.read_bytes()
    # meiva:note
    # The image's directory cannot take the temporary file the atomic write starts with.
    owned_image.parent.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            write_request(
                consumer, config_authority=config_authority, local_machine=machine_identity.name()
            )
    finally:
        owned_image.parent.chmod(0o700)
    assert owned_image.read_bytes() == saved


# meiva:note
# Two owners declare the same fields: attributing a field to one of them fails with a ValueError
# that no request handler anticipates.
@pytest.mark.parametrize("owner_names", [("validated_policy", "validated_policy_copy")])
@pytest.mark.parametrize("consumer", ["gateway", "host"])
def test_unexpected_owner_failure_keeps_original_exception_and_bytes(
    owned_image: Path,
    consumer: Literal["gateway", "host"],
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    saved = owned_image.read_bytes()
    with pytest.raises(ValueError, match="more than one declaration owner"):
        write_request(
            consumer, config_authority=config_authority, local_machine=machine_identity.name()
        )
    assert owned_image.read_bytes() == saved
