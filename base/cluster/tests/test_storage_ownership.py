"""Native PostgreSQL ownership: a receipt, never a pidfile, a listener or a prior boot, is what authorizes a stop, restart or signal."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

import psutil
import pytest

from base.clock import SYSTEM_TIMERS
from base.cluster import ownership, port_preflight
from base.cluster import postgres as pg
from base.native_process.ownership import OwnedProcess

# meiva:note
# The operating system the launch receipt is written for. `postgres.start` takes it as a value,
# and these suites drive the real server the way the Linux lane does (`NATIVE_TEST_MACOS` in
# `cli/commands/data_plane/tests/native_cluster.py` is the same stand-in): a receipt and the read
# that validates it must name one system, and the suite's is this one.
NATIVE_SYSTEM = "linux"

_PG_TEST_CHILD = Path(__file__).with_name("_pg_test_child.py")


@dataclass(frozen=True)
class _Uids:
    real: int


class _ScriptedProcess:
    def __init__(
        self,
        *,
        pid: int,
        name: str,
        cwd: Path,
        cmdline: list[str],
        refuses_uids: bool = False,
    ) -> None:
        self.pid = pid
        self._name = name
        self._cwd = cwd
        self._cmdline = cmdline
        self._refuses_uids = refuses_uids

    def uids(self) -> _Uids:
        if self._refuses_uids:
            raise psutil.AccessDenied(pid=self.pid)
        return _Uids(real=os.getuid())

    def status(self) -> str:
        return "running"

    def name(self) -> str:
        return self._name

    def cwd(self) -> str:
        return str(self._cwd)

    def cmdline(self) -> list[str]:
        return self._cmdline

    def is_running(self) -> bool:
        return True


@pytest.fixture
def pg_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A PostgreSQL data directory inside a private home.

    Custody keeps its receipt in `<data>/../run`, as a home's `pgdata` sits beside its `run`.
    A data directory directly under the session's shared base temp directory would hand every
    other test the receipt this one writes.
    """
    data = tmp_path_factory.mktemp("home") / "pgdata"
    data.mkdir()
    return data


def test_unrelated_listener_is_not_owned_by_a_valid_native_process() -> None:
    with pytest.raises(RuntimeError, match="does not belong"):
        port_preflight.verify_listener_custody(
            15433,
            has_identity=True,
            owner_live=True,
            owned_pids={123},
            listener_pids=[123, 456],
        )


def test_postmaster_pidfile_cannot_supply_missing_native_receipt(pg_data: Path) -> None:
    (pg_data / "postmaster.pid").write_text(f"123\n{pg_data}\n100\n")
    with pytest.raises(RuntimeError, match="no native launch receipt"):
        ownership.postgres(pg_data)


def _pg_receipt(data: Path, *, state: str = "captured", pid: int = 123) -> pg.Receipt:
    info = data.stat()
    system = sys.platform
    values = {
        "data": str(data.resolve()),
        "directory": [info.st_dev, info.st_ino],
        "port": 15433,
        "platform": system,
        "boot_id": pg.boot_id(system),
        "state": state,
        "owner": None
        if state in {"pending", "not-started"}
        else {
            "pid": pid,
            "create_time": 100.0,
            "starttime": 456,
        },
    }
    return pg.Receipt.model_validate_json(json.dumps(values))


# meiva:note
# The launch door states the platform the receipt is written for; this function never reads the
# host. Naming a system custody is not defined on reaches `boot_id`'s refusal, which is the proof
# the value came from the caller: a reader of its own platform would have launched instead.
def test_start_takes_the_system_its_caller_states(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()

    with pytest.raises(RuntimeError) as refusal:
        pg.start(
            data,
            15439,
            ["/bin/false"],
            {},
            ready=lambda: False,
            timeout=1.0,
            retained_children=[],
            timers=SYSTEM_TIMERS,
            system="windows",
        )

    assert "requires a supported POSIX platform" in str(refusal.value), (
        "postgres.start records the platform its caller stated, so a refusal naming that value "
        "proves the parameter reached the receipt's guard. If this test fails with a launch "
        f"attempt instead, start read the host again. refusal={refusal.value!r}"
    )


def test_an_unsupported_system_is_refused_before_any_boot_scope_is_read() -> None:
    with pytest.raises(RuntimeError) as refusal:
        pg.boot_id("windows")
    assert "requires a supported POSIX platform" in str(refusal.value), (
        "custody binds a pidfile to one native birth inside one boot scope, and that model is "
        "defined on macOS and Linux only: on any other system boot_id refuses instead of reading "
        "an identity no receipt in this repository was ever validated against. If this returns, an "
        "unsupported host adopts or rejects a postmaster on evidence nobody defined. "
        f"refusal={refusal.value!r}"
    )
    assert "'windows'" in str(refusal.value), (
        "the refusal names the system it rejected: an operator reading it is on a host this "
        "repository does not support, and a message that only says the platform is unsupported "
        "sends them looking for a bug in PostgreSQL instead of at the custody contract. "
        f"refusal={refusal.value!r}"
    )


def test_the_systems_boot_id_accepts_are_the_ones_a_receipt_can_name() -> None:
    receipt_systems = set(get_args(pg.Receipt.model_fields["platform"].annotation))
    assert set(pg.SUPPORTED_SYSTEMS) == receipt_systems, (
        "boot_id refuses a system outside SUPPORTED_SYSTEMS while Receipt.platform names the "
        "systems a receipt may record: if the two drift, a receipt read_receipt accepts can never "
        "be compared against a boot scope, and the failure surfaces at observe time on a host that "
        "has already launched a postmaster. Widen both together. "
        f"guard={sorted(pg.SUPPORTED_SYSTEMS)} receipt={sorted(receipt_systems)}"
    )


def test_pending_postgres_is_never_recovered_from_a_pidfile(pg_data: Path) -> None:
    pg.write(pg_data, _pg_receipt(pg_data, state="pending"))
    with pytest.raises(RuntimeError, match="unresolved"):
        pg.observe(pg_data)


def test_postgres_birth_uses_ticks_despite_wall_clock_drift(pg_data: Path) -> None:
    receipt = _pg_receipt(pg_data)
    info = pg_data.stat()
    assert (
        pg.verify_captured_process(
            pg_data,
            receipt,
            directory_inode=(info.st_dev, info.st_ino),
            process_name="postgres",
            argv=["postgres", "-D", str(pg_data)],
            cwd=pg_data,
            sid=123,
            captured_birth=OwnedProcess(123, 9000.0, 456),
            live=True,
        )
        == receipt.process()
    )
    with pytest.raises(RuntimeError, match="captured PostgreSQL"):
        pg.verify_captured_process(
            pg_data,
            receipt,
            directory_inode=(info.st_dev, info.st_ino),
            process_name="postgres",
            argv=["postgres", "-D", str(pg_data)],
            cwd=pg_data,
            sid=123,
            captured_birth=OwnedProcess(123, 100.0, 457),
            live=True,
        )


@pytest.mark.parametrize("change", ["directory", "pidfile", "pid", "port"])
def test_postgres_rejects_changed_file_binding(
    pg_data: Path,
    change: str,
) -> None:
    receipt = _pg_receipt(pg_data)
    record = pg_data / "postmaster.pid"
    record.write_text(f"123\n{pg_data}\n100\n15433\n")
    inode, header = pg.read_pidfile(pg_data, receipt)
    ready = receipt.model_copy(update={"state": "ready", "pidfile": inode, "header": header})
    if change == "pidfile":
        record.rename(pg_data / "original.pid")
        record.write_text("\n".join(header) + "\n")
    elif change == "directory":
        record.write_text(f"123\n{pg_data / 'foreign'}\n100\n15433\n")
    elif change == "pid":
        record.write_text(f"456\n{pg_data}\n100\n15433\n")
    else:
        record.write_text(f"123\n{pg_data}\n100\n15434\n")
    with pytest.raises(RuntimeError, match="pidfile"):
        pg.read_pidfile(pg_data, ready)


def test_prior_boot_never_donates_reused_pid_authority(pg_data: Path) -> None:
    receipt = _pg_receipt(pg_data, pid=os.getsid(os.getpid())).model_copy(
        update={"boot_id": "prior-boot"}
    )
    pg.write(pg_data, receipt)
    assert pg.observe(pg_data) is None


@pytest.mark.parametrize("name", ["postgres", "archive-command"])
def test_dead_postmaster_with_surviving_worker_refuses_restart(
    pg_data: Path,
    name: str,
) -> None:
    receipt = _pg_receipt(pg_data, pid=os.getsid(os.getpid()))
    worker = _ScriptedProcess(
        pid=os.getpid(),
        name=name,
        cwd=pg_data,
        cmdline=["postgres: checkpoint"],
    )
    with pytest.raises(RuntimeError, match="descendants"):
        pg.require_closed(pg_data, receipt, processes=[worker])


def test_closed_postgres_with_unknown_scan_refuses_restart(pg_data: Path) -> None:
    receipt = _pg_receipt(pg_data)
    unreadable = _ScriptedProcess(
        pid=999,
        name="postgres",
        cwd=pg_data,
        cmdline=["postgres"],
        refuses_uids=True,
    )
    with pytest.raises(psutil.AccessDenied):
        pg.require_closed(pg_data, receipt, processes=[unreadable])


def test_failed_pg_exec_is_not_started_but_ambiguous_spawn_stays_pending(
    pg_data: Path,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    pending = _pg_receipt(pg_data, state="pending")
    with pytest.raises(FileNotFoundError):
        pg.spawn_postgres(
            pg_data,
            pending,
            [str(pg_data / "missing-postgres")],
            {},
            retained_children=retained_children,
        )
    receipt = pg.read_receipt(pg_data)
    assert receipt is not None and receipt.state == "not-started"

    with pytest.raises(ValueError):
        pg.spawn_postgres(pg_data, pending, ["\x00"], {}, retained_children=retained_children)
    receipt = pg.read_receipt(pg_data)
    assert receipt is not None and receipt.state == "pending"


def test_real_cancellation_after_pg_spawn_retains_native_receipt(
    pg_data: Path,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    pending = _pg_receipt(pg_data, state="pending")
    pg.write(pg_data, pending)
    # meiva:note
    # Spawn the fixed Python child helper inside `pg.admission()` and deliver
    # SIGINT before `record_spawned_process` runs so receipt capture is verified
    # under real signal deferral.
    try:
        with (
            pytest.raises(KeyboardInterrupt, match="admission interrupted"),
            pg.admission(),
            (pg_data / "pg.log").open("ab") as log,
        ):
            child = subprocess.Popen(  # noqa: S603
                [sys.executable, str(_PG_TEST_CHILD)],
                cwd=pg_data,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            children.append(child)
            os.kill(os.getpid(), signal.SIGINT)
            pg.record_spawned_process(
                pg_data,
                pending,
                child,
                retained_children=retained_children,
            )
        receipt = pg.read_receipt(pg_data)
        assert receipt is not None and receipt.state == "captured"
        assert receipt.process().pid == children[0].pid and receipt.process().live()
        assert retained_children == children
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=5)


def test_retained_postgres_cannot_signal_replacement(
    pg_data: Path,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    (pg_data.parent / "run").mkdir(exist_ok=True)
    receipt = _pg_receipt(pg_data)
    with pytest.raises(RuntimeError, match="identity changed"):
        pg.verify_retained_owner(
            15433,
            OwnedProcess(123, 100.0, 456),
            OwnedProcess(123, 100.0, 457),
            receipt,
        )
    with pytest.raises(RuntimeError, match="identity changed"):
        pg.start(
            pg_data,
            15433,
            [],
            {},
            ready=lambda: True,
            expected=OwnedProcess(123, 100.0, 456),
            retained_children=retained_children,
            timers=SYSTEM_TIMERS,
            system=NATIVE_SYSTEM,
        )


@pytest.fixture
def retained_children() -> list[subprocess.Popen[bytes]]:
    return []


def test_stop_reaps_only_the_matching_home_child_after_native_closure() -> None:
    # meiva:note
    # Spawn two fixed Python child helpers so `reap_retained_child` can be
    # checked against one terminated PID while the sibling remains live.
    first = subprocess.Popen([sys.executable, str(_PG_TEST_CHILD)])  # noqa: S603
    other = subprocess.Popen([sys.executable, str(_PG_TEST_CHILD)])  # noqa: S603
    children: list[subprocess.Popen[bytes]] = [first, other]
    try:
        first.terminate()
        pg.reap_retained_child(first.pid, children)
        assert children == [other]
        assert other.poll() is None

        other.terminate()
        pg.reap_retained_child(other.pid, children)
        assert children == []
    finally:
        for child in (first, other):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


def test_failed_birth_capture_keeps_the_callers_spawned_handle(pg_data: Path) -> None:
    children: list[subprocess.Popen[bytes]] = []
    try:
        with pytest.raises(ValueError, match="native start ticks"):
            pg.spawn_postgres(
                pg_data,
                _pg_receipt(pg_data, state="pending"),
                [sys.executable, str(_PG_TEST_CHILD)],
                {},
                retained_children=children,
                captured_owner=OwnedProcess(123, 100.0, 0),
            )
        [child] = children
        assert child.poll() is None
        receipt = pg.read_receipt(pg_data)
        assert receipt is not None and receipt.state == "pending"
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=5)
