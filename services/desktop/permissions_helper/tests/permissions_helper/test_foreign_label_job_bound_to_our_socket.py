"""Permissions helper cases: a foreign-label job bound to our socket."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.host.permissions_helper import client
from base.host.permissions_helper.client import PermissionsHelperClient
from base.sessions.tests.refuse_helper import refuse_helper
from base.sessions.tests.scripted_helper import ScriptedHelper, serve
from services.desktop.permissions_helper import launchd_job
from services.desktop.permissions_helper.tests.support import helper_install_env, launchd_facts
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

    home = tmp_path / "home"
    mine = launchd_job.helper_job_label(home)
    foreign = write_agent_plist(agents, "com.meiva.permissions-helper.stray", sock)
    other_cluster = write_agent_plist(
        agents,
        "com.meiva.permissions-helper.meiva-other-abcdef12",
        str(tmp_path / ".." / "x") + "/.meiva-other/run/permissions-helper.18010.sock",
    )
    own = write_agent_plist(agents, mine, sock)

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="reconcile them externally"):
        lifecycle.refuse_foreign_label_jobs(
            facts=launchd_facts(home), socket=sock_path, current_label=mine
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
            facts=launchd_facts(tmp_path / "home"),
            socket=sock_path,
            current_label=launchd_job.helper_job_label(tmp_path / "home"),
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
            facts=launchd_facts(tmp_path / "home"),
            socket=sock_path,
            current_label=launchd_job.helper_job_label(tmp_path / "home"),
        )
        == []
    )
    assert other.exists()


def test_long_mac_ipc_path_refuses_before_building_or_native_jobs(
    tmp_path: Path,
) -> None:
    from services.desktop.permissions_helper import lifecycle

    long_home = tmp_path / ("h" * 100)
    recorded, build_env = fake_tools(home=long_home)
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="shorter cluster home"):
        lifecycle.converge(helper=refuse_helper, env=helper_install_env(build_env))
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
        lifecycle.helper_answers_ping(
            serve(ScriptedHelper(ping=reply)), timers=ManualTimers(), settle_seconds=0.0
        )


# meiva:note
# The job's identity comes from the account the entry hands in: the launchd domain is `gui/<uid>`
# and the plist lives in that account's LaunchAgents directory, under the home's own label.
def test_the_job_identity_follows_the_account_facts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    facts = launchd_job.LaunchdFacts(uid=777, platform="darwin", user_home=tmp_path / "somebody")

    assert facts.domain == "gui/777"
    assert launchd_job.helper_job_plist_path(home, facts=facts) == (
        tmp_path
        / "somebody"
        / "Library"
        / "LaunchAgents"
        / f"{launchd_job.helper_job_label(home)}.plist"
    )
