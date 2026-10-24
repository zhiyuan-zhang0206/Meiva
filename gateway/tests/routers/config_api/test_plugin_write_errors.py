from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self

import pytest
from httpx2 import Response
from pydantic import model_validator

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.host.system.tests.fake_commands import fake_host
from base.packages import plugin_config_admin as plugin_config
from base.packages import plugin_config_images
from base.packages.plugin_config_images import PluginConfigOwner
from gateway.tests.auth.gateway_config import gateway_test_client
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig
from ops.host_config import config_write_op
from ops.rpc_schemas import ConfigWriteOpResult


class ValidatedFleetConfig(FleetConfig):
    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.task_escalate_n < 1 or not self.task_maintenance_enabled:
            raise ValueError("candidate Fleet policy rejected")
        return self


@pytest.fixture
def owner_names() -> tuple[str, ...]:
    return ("meiva_fleet",)


@pytest.fixture
def owned_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_names: tuple[str, ...]
) -> Path:
    image = tmp_path / "images" / "config.json"
    image.parent.mkdir()
    image.write_text(ValidatedFleetConfig().model_dump_json())
    owners = {name: PluginConfigOwner(name, ValidatedFleetConfig, image) for name in owner_names}
    monkeypatch.setattr(plugin_config, "config_owners", lambda: owners)
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
            return client.put("/api/config", json={"task_escalate_n": -1 if bad_candidate else 7})
    return config_write_op(
        {"task_maintenance_enabled": not bad_candidate},
        local=False,
        authority=config_authority,
        local_machine=local_machine,
        host=fake_host(),
    )


@pytest.mark.parametrize("consumer", ["gateway", "host"])
def test_invalid_whole_candidate_is_rejected_without_writing(
    owned_image: Path,
    consumer: Literal["gateway", "host"],
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
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
    monkeypatch: pytest.MonkeyPatch,
    consumer: Literal["gateway", "host"],
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    concurrent = ValidatedFleetConfig(task_escalate_n=19).model_dump_json().encode()
    original_lock = plugin_config_images.file_lock

    @contextmanager
    def lock_after_competing_write(
        path: Path, *, timeout_s: float | None = None
    ) -> Generator[None]:
        with original_lock(path, timeout_s=timeout_s):
            owned_image.write_bytes(concurrent)
            yield

    monkeypatch.setattr(plugin_config_images, "file_lock", lock_after_competing_write)
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
    machine_identity: MachineIdentity,
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
@pytest.mark.parametrize("owner_names", [("meiva_fleet", "meiva_fleet_copy")])
@pytest.mark.parametrize("consumer", ["gateway", "host"])
def test_unexpected_owner_failure_keeps_original_exception_and_bytes(
    owned_image: Path,
    consumer: Literal["gateway", "host"],
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    saved = owned_image.read_bytes()
    with pytest.raises(ValueError, match="more than one declaration owner"):
        write_request(
            consumer, config_authority=config_authority, local_machine=machine_identity.name()
        )
    assert owned_image.read_bytes() == saved
