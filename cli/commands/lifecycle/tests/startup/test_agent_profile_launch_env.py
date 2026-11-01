# meiva:note
# An agent-profile unit on a remote-managed plane launches with the provider runner login.
#
# A remote-managed plane has no write generation, so the agents of a gateway-config unit dial the
# provider-provisioned runner role with the credential the gateway's `.env` records. The `.env` here
# is the real file of the test's home.

from pathlib import Path

from base.cluster.derive import project_runner_db_url
from base.host.env.dotenv_file import env_file_path, read_env_aliases
from cli.commands.data_plane.instance.launch_delivery import LaunchDelivery
from cli.commands.lifecycle import root_driver
from services.supervision.meiva_root.manifest import load_manifests
from services.supervision.meiva_root_glue.manifests import generate
from services.supervision.roster.service_spec import AGENT_RUNNER, ServiceSpec

_PROVIDER_URL = "postgresql://owner@db.example:5432/meiva"
# meiva:note
# The password is a literal of a fixture runner login that exists only in this test.
_RUNNER_PASSWORD = "fixture-runner-password"  # noqa: S105


def test_root_manifest_projects_runner_url_for_agent_profile(
    unit_home: Path, tmp_path: Path
) -> None:
    (unit_home / ".env").write_text(
        "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
        f"MEIVA_DB_URL={_PROVIDER_URL}\n"
        f"MEIVA_RUNNER_DB_PASSWORD={_RUNNER_PASSWORD}\n"
    )
    agent = ServiceSpec(
        session="agent-host",
        cmd=".venv/bin/python -m services.agent_runner.agent_host.daemon",
        capabilities=AGENT_RUNNER,
        requires_db=True,
        profile="agent",
    )
    ops = ServiceSpec(
        session="ops",
        cmd=".venv/bin/python -m services.agent_runner.agent_ops",
        capabilities=AGENT_RUNNER,
        requires_db=True,
    )
    projected = project_runner_db_url(_PROVIDER_URL, _RUNNER_PASSWORD)
    launch = LaunchDelivery(
        home=unit_home,
        remote_data_plane=True,
        environment={},
        env_file=read_env_aliases(env_file_path(unit_home)),
    )
    environments = {
        spec.session: root_driver.service_extra_env(spec, delivery=launch, roles=AGENT_RUNNER)
        for spec in (agent, ops)
    }
    path = generate(
        tmp_path / "manifest.json",
        capabilities=["agent-runner"],
        repo_root=tmp_path,
        specs=(agent, ops),
        environments=environments,
        home=unit_home,
    )
    units = {unit.id: unit for unit in load_manifests(path).units}
    assert dict(units["agent-host"].env) == {
        "MEIVA_PROCESS_PROFILE": "agent",
        "MEIVA_DB_URL": projected,
    }
    assert "MEIVA_DB_URL" not in dict(units["ops"].env)
