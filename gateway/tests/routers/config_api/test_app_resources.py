# meiva:note
# Configuration HTTP requests use the serving app's own resources: its home's `.env`, its
# machine and its host, never those of an app that served before it in the same process.

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config.tests.configuration import snapshot_process_config
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client


def _machine(name: str) -> ConfiguredMachineIdentity:
    machine = ConfiguredMachineIdentity(snapshot_process_config())
    machine.set(role="gateway", name=name)
    return machine


# meiva:note
# Home `index`: its `.env` names its own model and its own host concurrency.
def _home(root: Path, index: int) -> Path:
    home = root / f"home-{index}"
    home.mkdir(exist_ok=True)
    env = home / ".env"
    if not env.exists():
        env.write_text(f"MEIVA_MODEL=model-{index}\nMEIVA_OPS_CONCURRENCY={index + 3}\n")
    return home


# meiva:note
# Gateway `index` with its own machine. Neither is registered, so a request about the
# local machine is answered in process from that app's own authority and host. (Two lifespans
# cannot overlap: the telemetry pipeline is a process global, so the apps serve in turn.)
@contextmanager
def _gateway(root: Path, index: int) -> Generator[GatewayTestClient]:
    with gateway_test_client(
        home=_home(root, index), machine=_machine(f"gateway-{index}")
    ) as client:
        yield client


def test_config_read_uses_each_apps_home(tmp_path: Path) -> None:
    for index in (1, 0, 1):
        with _gateway(tmp_path, index) as client:
            response = client.get("/api/config")
        assert response.status_code == 200, response.text
        fields = {field["name"]: field for field in response.json()["fields"]}
        assert fields["ops_concurrency"]["current_value"] == index + 3
        assert fields["llm_model"]["current_value"] == f"model-{index}"
        assert response.json()["machine_capabilities"] == ["gateway"]
