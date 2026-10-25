"""Permissions helper cases: a foreign-label job bound to our socket."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.permissions_helper import client
from base.host.permissions_helper.client import PermissionsHelperClient
from base.sessions.tests.refuse_helper import refuse_helper
from base.sessions.tests.scripted_helper import ScriptedHelper, serve
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_helper as fake_helper,
)
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_tools,
    retire_env,
    write_agent_plist,
)


def test_foreign_label_job_bound_to_our_socket_is_refused(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    agents, sock_path = retire_env(tmp_path)
    sock = str(sock_path)

    mine = lifecycle.label(tmp_path)
    foreign = write_agent_plist(agents, "com.meiva.permissions-helper.stray", sock)
    other_cluster = write_agent_plist(
        agents,
        "com.meiva.permissions-helper.meiva-other-abcdef12",
        str(tmp_path / ".." / "x") + "/.meiva-other/run/permissions-helper.18010.sock",
    )
    own = write_agent_plist(agents, mine, sock)

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="reconcile them externally"):
        lifecycle.refuse_foreign_label_jobs(
            agents=agents,
            socket=sock_path,
            current_label=mine,
        )
    assert foreign.exists()
    # ...while other clusters' jobs and our own are untouched.
    assert other_cluster.exists()
    assert own.exists()


def test_foreign_label_refusal_holds_when_the_job_is_not_loaded(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    agents, sock_path = retire_env(tmp_path)
    sock = str(sock_path)

    write_agent_plist(agents, "com.meiva.permissions-helper.stray", sock)

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="reconcile them externally"):
        lifecycle.refuse_foreign_label_jobs(
            agents=agents,
            socket=sock_path,
            current_label=lifecycle.label(tmp_path),
        )
    assert (agents / "com.meiva.permissions-helper.stray.plist").exists()


def test_unrelated_cluster_plists_are_left_alone(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    agents, sock_path = retire_env(tmp_path)

    other = write_agent_plist(
        agents,
        "com.meiva.permissions-helper.meiva-demo-1234abcd",
        str(tmp_path / ".." / "z") + "/.meiva-demo/run/permissions-helper.1234.sock",
        env_key="MEIVA_PERMISSIONS_HELPER_SOCKET",
    )

    assert (
        lifecycle.foreign_label_plists(
            agents=agents,
            socket=sock_path,
            current_label=lifecycle.label(tmp_path),
        )
        == []
    )
    assert other.exists()


def test_long_mac_ipc_path_refuses_before_building_or_native_jobs(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    long_socket = tmp_path / ("s" * 104)
    recorded, build_env = fake_tools()
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="shorter cluster home"):
        lifecycle.converge(
            helper=refuse_helper,
            env=lifecycle.InstallEnv(
                home=tmp_path,
                socket=long_socket,
                build=build_env,
            ),
        )
    assert recorded == []


def test_overlong_socket_is_typed_unavailability() -> None:
    with pytest.raises(client.PermissionsHelperError, match="not reachable"):
        PermissionsHelperClient("/" + "x" * 200).ping()


@pytest.mark.parametrize("capability", ["root_stop_intent_v1", "helper_shutdown_v1"])
def test_helper_ping_refuses_missing_stop_capability(capability: str) -> None:
    from services.desktop.permissions_helper import lifecycle

    reply = {"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True}
    reply.pop(capability)
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="upgrade its signed artifact"):
        lifecycle.helper_answers_ping(serve(ScriptedHelper(ping=reply)), settle_seconds=0.0)
