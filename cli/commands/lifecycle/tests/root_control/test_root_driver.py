# meiva:note
# The shell around the one application root, against a real control socket and real files.
#
# What a snapshot means is asserted on the plans (`test_reconcile_plan.py`, `test_poll_plan.py`);
# here the shell is shown to dial the root, execute those plans and leave the home as they say.
# The scripted root (`scripted_root.py`) is a unix socket under the home that speaks the K1 wire.

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from functools import partial
from pathlib import Path

import psutil
import pytest

from base.clock import SYSTEM_TIMERS
from base.host.permissions_helper.client import PermissionsHelperError, RootStatus
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.native_process.root_control import instance_lock
from base.paths import meiva_home, root_manifests_path, root_run_dir
from base.sessions.env_forwarding import managed_service_env
from base.sessions.tests.refuse_helper import refuse_helper
from base.sessions.tests.scripted_helper import ScriptedHelper, serve
from cli.commands.lifecycle import root_driver as driver
from cli.commands.lifecycle.root_control.launch_plan import RootLaunch, plan_root_launch
from cli.commands.lifecycle.root_control.snapshot import root_units
from cli.commands.lifecycle.tests.root_control.snapshots import snapshot_with_root
from cli.commands.lifecycle.tests.root_control.support import (
    LAUNCHD_FACTS,
    custody_helper,
    fake_interpreter,
    git_repo,
    launch_delivery,
    launch_inputs,
    refuse_launchctl,
    service,
    this_process,
)
from cli.commands.lifecycle.tests.runtime_at import runtime_at
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root
from services.supervision.meiva_root.manifest import UnitManifest
from services.supervision.roster import composition
from services.supervision.roster.service_spec import ServiceSpec

pytestmark = pytest.mark.real_service_readiness_gate

ROLES = frozenset({"gateway"})


def launch_of(home: Path, interpreter: Path) -> RootLaunch:
    return plan_root_launch(
        interpreter=interpreter,
        cwd=home,
        run_dir=root_run_dir(home),
        manifests=root_manifests_path(home),
    )


# meiva:note
# ── custody ──────────────────────────────────────────────────────────────────


def test_the_custody_refusal_names_the_recovery(short_tmp: Path) -> None:
    me = this_process()
    keeper_of_another_root = ScriptedHelper(
        ping={
            "pong": True,
            "root_stop_intent_v1": True,
            "helper_shutdown_v1": True,
            "pid": os.getppid(),
        },
        root_status={"pid": me.pid + 1, "run_dir": str(root_run_dir(short_tmp))},
    )

    with pytest.raises(driver.RootDriverError) as refusal:
        driver.require_root_owner(
            snapshot_with_root(me), helper=serve(keeper_of_another_root), home=short_tmp
        )

    assert "outside this home's signed-helper custody" in str(refusal.value)
    assert "meiva start" in str(refusal.value)


def test_a_root_the_helper_started_in_this_home_is_in_custody(short_tmp: Path) -> None:
    me = this_process()

    driver.require_root_owner(
        snapshot_with_root(me), helper=serve(custody_helper(short_tmp, me)), home=short_tmp
    )


def test_a_helper_keeping_its_root_in_another_home_does_not_have_custody_here(
    short_tmp: Path, tmp_path: Path
) -> None:
    me = this_process()
    other_home = custody_helper(tmp_path, me)

    with pytest.raises(driver.RootDriverError, match="signed-helper custody"):
        driver.require_root_owner(snapshot_with_root(me), helper=serve(other_home), home=short_tmp)


def test_a_helper_lacking_the_retirement_protocol_has_no_custody_to_offer(short_tmp: Path) -> None:
    me = this_process()
    old_helper = ScriptedHelper(ping={"pong": True, "pid": os.getppid()})

    with pytest.raises(driver.RootDriverError, match="lacks lifecycle protocols"):
        driver.require_root_owner(snapshot_with_root(me), helper=serve(old_helper), home=short_tmp)


# meiva:note
# The dormant-root stop over a macOS account whose launchd the test answers (or refuses) through
# `run`.
_stop_dormant = partial(
    driver.stop_dormant_helper_root,
    float("inf"),
    facts=LAUNCHD_FACTS,
    run=refuse_launchctl,
    timers=SYSTEM_TIMERS,
)


def test_a_platform_without_a_helper_has_no_dormant_helper_root_to_stop(short_tmp: Path) -> None:
    _stop_dormant(helper=None, home=short_tmp)


def test_stop_without_root_ipc_cancels_pending_helper_restart(short_tmp: Path) -> None:
    keeper = {"stop_requested": False}

    def stop() -> RootStatus:
        keeper["stop_requested"] = True
        return {"state": "stopped", "seeded": True, "restarts": 1, "stop_requested": True}

    helper = ScriptedHelper(
        ping={"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True},
        root_status={"state": "backoff", "seeded": True, "restarts": 1, "stop_requested": False},
        stop_root=stop,
    )

    _stop_dormant(helper=serve(helper), home=short_tmp)

    assert keeper["stop_requested"]


def test_a_keeper_that_holds_a_root_without_ipc_is_not_stopped_blind(short_tmp: Path) -> None:
    helper = ScriptedHelper(
        root_status={
            "state": "running",
            "seeded": True,
            "restarts": 0,
            "stop_requested": False,
            "pid": 4,
        }
    )

    with pytest.raises(driver.RootDriverError, match="custody requires recovery"):
        _stop_dormant(helper=serve(helper), home=short_tmp)


@pytest.mark.parametrize("loaded", [False, True])
def test_unusable_helper_socket_requires_positive_native_absence(
    short_tmp: Path, loaded: bool
) -> None:
    # meiva:note
    # launchd's answer is the one fact no local resource can produce: whether the helper job is
    # loaded. `retirement_query` is that answer.
    def runner(
        cmd: Sequence[str],
        /,
        *,
        timeout: float,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout, capture_output
        assert cmd[2].startswith(f"gui/{LAUNCHD_FACTS.uid}/")
        if loaded:
            return subprocess.CompletedProcess(cmd, 0, b"state = spawn scheduled\n", b"")
        return subprocess.CompletedProcess(cmd, 113, b"", b"Could not find service")

    unavailable = serve(ScriptedHelper(root_status=PermissionsHelperError("AF_UNIX path too long")))

    if loaded:
        with pytest.raises(driver.RootDriverError, match="custody is unavailable"):
            _stop_dormant(helper=unavailable, home=short_tmp, run=runner)
    else:
        _stop_dormant(helper=unavailable, home=short_tmp, run=runner)


# meiva:note
# ── absence ──────────────────────────────────────────────────────────────────


def test_obsolete_custody_records_do_not_block_an_absent_root(short_tmp: Path) -> None:
    custody = root_run_dir(short_tmp) / "custody"
    custody.mkdir(parents=True)
    (custody / "gateway.json").write_text("unknown")

    driver.require_root_absent(short_tmp)


def test_a_held_instance_lock_is_not_absence(short_tmp: Path) -> None:
    held = instance_lock.acquire_instance_lock(root_run_dir(short_tmp))
    try:
        with pytest.raises(instance_lock.AlreadyRunningError):
            driver.require_root_absent(short_tmp)
    finally:
        instance_lock.release_instance_lock(held)


def test_live_admission_requires_positive_absence(short_tmp: Path, tmp_path: Path) -> None:
    repo = git_repo(tmp_path / "repo")
    held = instance_lock.acquire_instance_lock(root_run_dir(short_tmp))
    try:
        with pytest.raises(instance_lock.AlreadyRunningError):
            driver.admit_live_start(
                (),
                repo,
                ROLES,
                reconcile=True,
                helper=refuse_helper,
                inputs=launch_inputs(short_tmp),
            )
    finally:
        instance_lock.release_instance_lock(held)


def test_with_no_root_and_no_holder_the_start_is_cold(short_tmp: Path, tmp_path: Path) -> None:
    repo = git_repo(tmp_path / "repo")

    live = driver.admit_live_start(
        (), repo, ROLES, reconcile=True, helper=None, inputs=launch_inputs(short_tmp)
    )

    assert live is False


def test_mac_boot_tail_does_not_read_or_notify(short_tmp: Path) -> None:
    # meiva:note
    # A root read would raise: no root answers in this home.
    driver.complete_boot_start(
        helper=refuse_helper, host=fake_host(platform=DARWIN_ARM64), home=short_tmp
    )


# meiva:note
# ── the launch generation ────────────────────────────────────────────────────


def requested_launch(
    home: Path, repo: Path
) -> tuple[tuple[ServiceSpec, ...], dict[str, object], dict[str, str]]:
    roster = (service(),)
    inputs = launch_inputs(home)
    manifest = driver.launch_manifest(
        roster,
        repo,
        roles=ROLES,
        env=managed_service_env(inputs.service_path, environ=inputs.environment),
        inputs=inputs,
    )
    return roster, manifest, driver.unit_digests(manifest)


def test_an_identical_live_generation_is_admitted(short_tmp: Path, tmp_path: Path) -> None:
    repo = git_repo(tmp_path / "repo")
    roster, manifest, digests = requested_launch(short_tmp, repo)
    row = UnitRow("gateway", manifest_digest=digests["gateway"])

    with scripted_root(short_tmp, [row], launch_digest=str(manifest["launch_digest"])):
        live = driver.admit_live_start(
            roster,
            repo,
            ROLES,
            reconcile=True,
            helper=None,
            inputs=launch_inputs(short_tmp),
        )

    assert live is True


@pytest.mark.parametrize("what", ["unit manifest", "launch inputs", "unit beyond the request"])
def test_a_live_generation_that_differs_is_refused_with_the_stop_that_resolves_it(
    short_tmp: Path, tmp_path: Path, what: str
) -> None:
    repo = git_repo(tmp_path / "repo")
    roster, manifest, digests = requested_launch(short_tmp, repo)
    rows = [UnitRow("gateway", manifest_digest=digests["gateway"])]
    launch_digest = str(manifest["launch_digest"])
    if what == "unit manifest":
        rows = [UnitRow("gateway", manifest_digest="old-digest")]
    elif what == "launch inputs":
        launch_digest = "0" * 64
    else:
        rows.append(UnitRow("agent-host", manifest_digest="m"))

    with (
        scripted_root(short_tmp, rows, launch_digest=launch_digest),
        pytest.raises(driver.RootDriverError, match="run meiva stop before starting"),
    ):
        driver.admit_live_start(
            roster,
            repo,
            ROLES,
            reconcile=True,
            helper=None,
            inputs=launch_inputs(short_tmp),
        )


@pytest.mark.parametrize("removed", [False, True])
def test_generation_change_refuses_without_signal_or_seed_publication(
    short_tmp: Path, tmp_path: Path, removed: bool
) -> None:
    repo = git_repo(tmp_path / "repo")
    roster, manifest, digests = requested_launch(short_tmp, repo)
    rows = [UnitRow("gateway", state="stopped", manifest_digest=digests["gateway"])]
    if not removed:
        rows = [UnitRow("gateway", state="stopped", manifest_digest="old-digest")]
    else:
        rows.append(UnitRow("agent-host", manifest_digest="m"))
    published = root_manifests_path(short_tmp)
    published.parent.mkdir(parents=True, exist_ok=True)
    published.write_text("old generation")
    # meiva:note
    # The recorded root is this process and its helper is the parent, so the custody check
    # passes and only the generation comparison can refuse. The helper scripts no `stop_root`
    # or `seed_root`: a refused generation that signalled either would fail the test.
    helper = custody_helper(short_tmp, this_process())

    with scripted_root(short_tmp, rows, launch_digest=str(manifest["launch_digest"])) as root:
        result = driver.launch_service_tree(
            roster,
            repo,
            ROLES,
            reconcile=True,
            retained_children=[],
            helper=serve(helper),
            runtime=runtime_at(repo),
            honor_restart_budget=False,
            inputs=launch_inputs(short_tmp),
            timers=SYSTEM_TIMERS,
        )

    assert result.failed == ("meiva-gateway",)
    assert published.read_text() == "old generation"
    assert root.units["gateway"].state == "stopped"


def test_an_identical_generation_is_reused_and_only_its_stopped_units_are_brought_up(
    short_tmp: Path, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path / "repo")
    roster, manifest, digests = requested_launch(short_tmp, repo)
    row = UnitRow("gateway", state="stopped", manifest_digest=digests["gateway"])

    with scripted_root(short_tmp, [row], launch_digest=str(manifest["launch_digest"])) as root:
        result = driver.launch_service_tree(
            roster,
            repo,
            ROLES,
            reconcile=True,
            retained_children=[],
            helper=None,
            runtime=runtime_at(repo),
            honor_restart_budget=False,
            inputs=launch_inputs(short_tmp),
            timers=SYSTEM_TIMERS,
        )

    assert result == driver.LaunchOutcome(roster, ())
    assert root.units["gateway"].state == "running"


def test_a_core_unit_the_root_refuses_fails_the_whole_launch(
    short_tmp: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = git_repo(tmp_path / "repo")
    roster, manifest, digests = requested_launch(short_tmp, repo)
    row = UnitRow("gateway", state="stopped", manifest_digest=digests["gateway"], refuse_up=True)

    with scripted_root(short_tmp, [row], launch_digest=str(manifest["launch_digest"])):
        result = driver.launch_service_tree(
            roster,
            repo,
            ROLES,
            reconcile=True,
            retained_children=[],
            helper=None,
            runtime=runtime_at(repo),
            honor_restart_budget=False,
            inputs=launch_inputs(short_tmp),
            timers=SYSTEM_TIMERS,
        )

    assert result == driver.LaunchOutcome(roster, ("meiva-gateway",))
    assert "meiva-root tree bring-up failed: up gateway failed" in capsys.readouterr().err


# meiva:note
# A root that answers with a status the commands cannot read fails the launch the way an
# unreachable one does: reported on stderr as a failed bring-up, never a traceback.
def test_a_root_with_an_unreadable_status_fails_the_launch_without_raising(
    short_tmp: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = git_repo(tmp_path / "repo")
    roster, manifest, _digests = requested_launch(short_tmp, repo)

    with scripted_root(
        short_tmp, [UnitRow("gateway")], launch_digest=str(manifest["launch_digest"])
    ) as root:
        root.raw_unit_rows = [{"id": "gateway", "state": "running", "last_error": 5}]
        result = driver.launch_service_tree(
            roster,
            repo,
            ROLES,
            reconcile=True,
            retained_children=[],
            helper=None,
            runtime=runtime_at(repo),
            honor_restart_budget=False,
            inputs=launch_inputs(short_tmp),
            timers=SYSTEM_TIMERS,
        )

    assert result == driver.LaunchOutcome(roster, ("meiva-gateway",))
    assert "meiva-root answered with an unreadable status" in capsys.readouterr().err


def test_a_unit_that_exits_again_after_up_is_reported_with_the_roots_last_error(
    short_tmp: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = git_repo(tmp_path / "repo")
    roster, manifest, digests = requested_launch(short_tmp, repo)
    row = UnitRow(
        "gateway",
        state="stopped",
        manifest_digest=digests["gateway"],
        last_error="unexpected exit: signal 9",
        stays_down=True,
    )

    with scripted_root(short_tmp, [row], launch_digest=str(manifest["launch_digest"])):
        result = driver.launch_service_tree(
            roster,
            repo,
            ROLES,
            reconcile=True,
            retained_children=[],
            helper=None,
            runtime=runtime_at(repo),
            honor_restart_budget=False,
            inputs=launch_inputs(short_tmp),
            timers=SYSTEM_TIMERS,
        )

    assert result == driver.LaunchOutcome(roster, ("meiva-gateway",))
    assert "meiva-gateway: unexpected exit: signal 9" in capsys.readouterr().err


def test_the_launch_digest_is_published_but_the_launch_environment_is_not(
    short_tmp: Path, tmp_path: Path
) -> None:
    from services.supervision.meiva_root_glue.manifests import write_manifest

    repo = git_repo(tmp_path / "repo")
    inputs = launch_inputs(short_tmp)
    secret = {"MEIVA_HOME": str(short_tmp), "MEIVA_TOKEN": "test-private-value"}

    manifest = driver.launch_manifest((service(),), repo, roles=ROLES, env=secret, inputs=inputs)
    published = write_manifest(tmp_path / "manifest.json", manifest)

    assert len(str(manifest["launch_digest"])) == 64
    assert b"test-private-value" not in published.read_bytes()
    assert json.loads(published.read_text())["launch_digest"] == manifest["launch_digest"]


# meiva:note
# ── the unit environment ─────────────────────────────────────────────────────


def test_collector_config_bytes_change_the_live_unit_generation(
    unit_home: Path, tmp_path: Path
) -> None:
    from base.paths import otel_collector_config

    config = otel_collector_config(unit_home)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("receivers: {otlp: {}}\n")
    collector = next(
        s for s in composition.build_services(home=unit_home) if s.session == "otel-collector"
    )

    before = driver.unit_digests(
        driver.tree_manifest(
            (collector,), tmp_path, roles=ROLES, delivery=launch_delivery(tmp_path), home=unit_home
        )
    )
    unchanged = driver.unit_digests(
        driver.tree_manifest(
            (collector,), tmp_path, roles=ROLES, delivery=launch_delivery(tmp_path), home=unit_home
        )
    )
    config.write_text("receivers: {otlp: {protocols: {http: {}}}}\n")
    after = driver.unit_digests(
        driver.tree_manifest(
            (collector,), tmp_path, roles=ROLES, delivery=launch_delivery(tmp_path), home=unit_home
        )
    )

    assert before == unchanged
    assert before["otel-collector"] != after["otel-collector"]


def test_plugin_birth_config_is_part_of_the_existing_unit_generation(tmp_path: Path) -> None:
    from dataclasses import replace

    from pydantic import BaseModel, ConfigDict

    from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV

    class Config(BaseModel):
        model_config = ConfigDict(frozen=True)
        interval: float = 300

    def unit_of(spec: ServiceSpec) -> UnitManifest:
        manifest = driver.tree_manifest(
            (spec,), tmp_path, roles=ROLES, delivery=launch_delivery(tmp_path), home=meiva_home()
        )
        from typing import Any

        units_obj = manifest["units"]
        assert isinstance(units_obj, list)
        units_any: Any = units_obj
        row_obj: object = units_any[0]
        assert isinstance(row_obj, dict)
        row_any: Any = row_obj
        row: dict[str, object] = row_any
        return UnitManifest.from_mapping(row, origin="test")

    base = replace(service(), plugin_config=("probe", Config(interval=40)))
    same = replace(base, plugin_config=("probe", Config(interval=40)))
    changed = replace(base, plugin_config=("probe", Config(interval=41)))
    units = [unit_of(row) for row in (base, same, changed)]

    assert SERVICE_PLUGIN_CONFIG_ENV in dict(units[0].env)
    assert units[0].digest() == units[1].digest()
    assert units[0].digest() != units[2].digest()


# meiva:note
# ── bringing a root up ───────────────────────────────────────────────────────


def test_mac_helper_protocol_refuses_fallback(short_tmp: Path) -> None:
    silent = ScriptedHelper(ping=PermissionsHelperError("no socket"))
    launch = launch_of(short_tmp, Path(sys.executable))

    with pytest.raises(RuntimeError, match="root_stop_intent_v1"):
        driver.bring_up_root(
            launch,
            driver.root_client(short_tmp, environ=os.environ),
            {},
            retained_children=[],
            helper=serve(silent),
            timers=SYSTEM_TIMERS,
        )


def test_helper_seed_is_durable_before_wire_start(short_tmp: Path) -> None:
    launch = launch_of(short_tmp, Path(sys.executable))
    launch.run_dir.mkdir(parents=True)
    env = {"MEIVA_HOME": str(short_tmp)}

    def seed(config: object) -> RootStatus:
        path = launch.run_dir / "seed.json"
        assert json.loads(path.read_text()) == config
        assert path.stat().st_mode & 0o777 == 0o600
        return {"state": "running", "seeded": True, "restarts": 0, "stop_requested": False}

    driver.seed_via_helper(
        launch, env, helper=serve(ScriptedHelper(seed_root=seed)), timers=SYSTEM_TIMERS
    )


def test_seed_wait_follows_the_keeper_until_it_reports_the_root_running(short_tmp: Path) -> None:
    launch = launch_of(short_tmp, Path(sys.executable))
    launch.run_dir.mkdir(parents=True)
    readings = iter(["backoff", "backoff", "running"])

    def reading() -> RootStatus:
        return {"state": next(readings), "seeded": True, "restarts": 0, "stop_requested": False}

    helper = ScriptedHelper(seed_root=reading(), root_status=reading)

    driver.seed_via_helper(launch, {}, helper=serve(helper), interval_s=0.01, timers=SYSTEM_TIMERS)

    # meiva:note
    # The wait read every scripted answer: it did not stop at the first non-final one.
    assert next(readings, None) is None


def test_seed_wait_reports_a_keeper_that_stopped_restarting_root(short_tmp: Path) -> None:
    launch = launch_of(short_tmp, Path(sys.executable))
    launch.run_dir.mkdir(parents=True)
    exited: RootStatus = {
        "state": "exited",
        "seeded": True,
        "restarts": 2,
        "stop_requested": False,
        "last_exit": {"kind": "crash", "code": 1, "at": 0.0},
    }

    with pytest.raises(driver.RootDriverError, match="stopped restarting"):
        driver.seed_via_helper(
            launch, {}, helper=serve(ScriptedHelper(seed_root=exited)), timers=SYSTEM_TIMERS
        )


def test_seed_wait_gives_up_on_a_keeper_that_never_settles(short_tmp: Path) -> None:
    launch = launch_of(short_tmp, Path(sys.executable))
    launch.run_dir.mkdir(parents=True)
    backoff: RootStatus = {
        "state": "backoff",
        "seeded": True,
        "restarts": 1,
        "stop_requested": False,
    }

    with pytest.raises(driver.RootDriverError, match=r"within 0s \(keeper state=backoff\)"):
        driver.seed_via_helper(
            launch,
            {},
            helper=serve(ScriptedHelper(seed_root=backoff)),
            timeout_s=0.0,
            interval_s=0.01,
            timers=SYSTEM_TIMERS,
        )


def test_await_root_status_returns_the_snapshot_of_the_serving_root(short_tmp: Path) -> None:
    launch = launch_of(short_tmp, Path(sys.executable))

    with scripted_root(short_tmp, [UnitRow("gateway")]):
        snapshot = driver.await_root_status(
            driver.root_client(short_tmp, environ=os.environ), launch, timers=SYSTEM_TIMERS
        )

    assert list(root_units(snapshot)) == ["gateway"]


def test_a_root_that_never_serves_fails_with_the_tail_of_its_log(short_tmp: Path) -> None:
    launch = launch_of(short_tmp, Path(sys.executable))
    launch.run_dir.mkdir(parents=True)
    launch.stderr_log.write_text("first\nboom: cannot bind\n")

    with pytest.raises(driver.RootDriverError, match="did not become ready within 0s") as failure:
        driver.await_root_status(
            driver.root_client(short_tmp, environ=os.environ),
            launch,
            timeout_s=0.0,
            interval_s=0.01,
            timers=SYSTEM_TIMERS,
        )

    assert "boom: cannot bind" in str(failure.value)


def test_a_root_that_exits_before_serving_fails_with_its_code_and_log(short_tmp: Path) -> None:
    launch = launch_of(short_tmp, fake_interpreter(short_tmp, "echo 'no such wiring' >&2; exit 3"))
    launch.run_dir.mkdir(parents=True)
    children: list[subprocess.Popen[bytes]] = []

    with pytest.raises(driver.RootDriverError, match=r"exited before serving \(rc=3\)") as failure:
        driver.bring_up_root(
            launch,
            driver.root_client(short_tmp, environ=os.environ),
            {"PATH": os.environ["PATH"]},
            retained_children=children,
            helper=None,
            timers=SYSTEM_TIMERS,
        )

    assert "no such wiring" in str(failure.value)
    # meiva:note
    # retained even though the bring-up failed
    assert len(children) == 1


def test_a_direct_launch_retains_its_child_and_cannot_have_it_reaped_by_an_unrelated_subprocess(
    short_tmp: Path,
) -> None:
    launch = launch_of(short_tmp, fake_interpreter(short_tmp, "exec sleep 30"))
    launch.run_dir.mkdir(parents=True)
    children: list[subprocess.Popen[bytes]] = []

    with scripted_root(short_tmp):
        snapshot = driver.bring_up_root(
            launch,
            driver.root_client(short_tmp, environ=os.environ),
            {"PATH": os.environ["PATH"]},
            retained_children=children,
            helper=None,
            timers=SYSTEM_TIMERS,
        )
    assert snapshot["root"]["pid"] == os.getpid()
    assert len(children) == 1
    pid = children[0].pid
    try:
        gc.collect()
        children[0].terminate()
        deadline = time.monotonic() + 5
        while psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "child did not exit"
            time.sleep(0.01)
        subprocess.run([sys.executable, "--version"], check=True, capture_output=True, timeout=5)
        assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    finally:
        children[0].wait(timeout=5)


# meiva:note
# ── reconciling units ────────────────────────────────────────────────────────


# meiva:note
# Reconcile `requested` against a scripted root; the state of each unit afterwards.
def reconcile(
    home: Path, rows: list[UnitRow], *requested: str, honor_restart_budget: bool
) -> dict[str, str]:
    roster = tuple(service(session) for session in requested)
    with scripted_root(home, rows):
        client = driver.root_client(home, environ=os.environ)
        snapshot = driver.root_status(client)
        assert snapshot is not None
        refreshed = driver.reconcile_units(
            roster, client, snapshot, honor_restart_budget=honor_restart_budget
        )
    return {unit_id: row["state"] for unit_id, row in root_units(refreshed).items()}


# meiva:note
# Explicit `meiva start` is the operator's way to revive an exhausted unit.
def test_an_operator_start_brings_up_a_unit_whose_restart_budget_is_spent(short_tmp: Path) -> None:
    spent = UnitRow("gateway", state="stopped", restart_limit_reached=True)

    states = reconcile(short_tmp, [spent], "gateway", honor_restart_budget=False)

    assert states == {"gateway": "running"}


def test_a_boot_attempt_leaves_a_unit_down_whose_restart_budget_is_spent(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spent = UnitRow("gateway", state="stopped", restart_limit_reached=True)

    states = reconcile(short_tmp, [spent], "gateway", honor_restart_budget=True)

    assert states == {"gateway": "stopped"}
    assert "meiva-gateway has an open restart breaker" in capsys.readouterr().out


def test_a_boot_attempt_still_brings_up_a_unit_with_budget_left(short_tmp: Path) -> None:
    states = reconcile(
        short_tmp, [UnitRow("gateway", state="stopped")], "gateway", honor_restart_budget=True
    )

    assert states == {"gateway": "running"}


def test_a_core_unit_the_root_refuses_fails_the_reconcile(short_tmp: Path) -> None:
    refused = UnitRow("gateway", state="stopped", refuse_up=True)

    with pytest.raises(driver.RootDriverError, match="up gateway failed"):
        reconcile(short_tmp, [refused], "gateway", honor_restart_budget=False)


def test_an_optional_unit_the_root_refuses_is_reported_and_the_rest_come_up(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        UnitRow("browser-mcp", state="stopped", refuse_up=True),
        UnitRow("gateway", state="stopped"),
    ]

    states = reconcile(short_tmp, rows, "browser-mcp", "gateway", honor_restart_budget=False)

    assert states == {"browser-mcp": "stopped", "gateway": "running"}
    assert "optional service browser-mcp" in capsys.readouterr().err
