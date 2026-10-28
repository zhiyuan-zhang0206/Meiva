from __future__ import annotations

import datetime as dt
import json
import os
import random
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from base.clock.ids import new_uuid
from base.clock.tests.seeded_rng import seeded_rng
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.sessions import coding_session_owner as owner
from base.sessions import coding_session_owner_record as record_codec

NOW = dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC)
WRITER = AtomicWriter(SYSTEM_FS)
RNG = seeded_rng()


# meiva:note
# The coding sessions a cleanup sees: a set of live names, and a record of what was stopped. A stop is always the hard one the owner makes (not graceful, marked expected): the fake refuses any other, so every cleanup test also pins how the owner stops a session.
class _FakeSessions:
    def __init__(self, live: set[str] | None = None, stopped: list[str] | None = None) -> None:
        self.live: set[str] = live if live is not None else set()
        self.stopped: list[str] = stopped if stopped is not None else []

    def list_sessions(self, prefix: str = "") -> list[str]:
        return sorted(name for name in self.live if name.startswith(prefix))

    def has_session(self, name: str) -> bool:
        return name in self.live

    def kill_session(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0, expected: bool = False
    ) -> tuple[bool, str]:
        assert not graceful and expected, "the owner stops a session hard, as an expected stop"
        self.stopped.append(name)
        self.live.discard(name)
        return True, "forced"


def _key(tmp_path: Path, workspace: str = "workspace") -> owner.CodingSessionKey:
    path = tmp_path / workspace
    path.mkdir(parents=True, exist_ok=True)
    return owner.canonical_key(path, tool="codex", cluster=tmp_path / "cluster")


def _launch(
    key: owner.CodingSessionKey,
    *,
    agent_id: int,
    now: dt.datetime = NOW,
    live: set[str] | None = None,
    terminated: set[int] | None = None,
    stopped: list[str] | None = None,
    takeover: bool = False,
    rng: random.Random = RNG,
) -> owner.CodingSessionOwner:
    terminated_agents: set[int] = terminated if terminated is not None else set()
    workspace = Path(key.workspace)
    return owner.launch_generation(
        key,
        owner_agent_id=agent_id,
        tasks_file=None if takeover else workspace / "tasks.md",
        work_file=None if takeover else workspace / "work.md",
        ttl_seconds=3600,
        writer=WRITER,
        now=now,
        rng=rng,
        sessions=_FakeSessions(live, stopped),
        owner_terminated=lambda agent: agent in terminated_agents,
    )


def _publish(
    record: owner.CodingSessionOwner,
    session_id: int,
    *,
    supervised: bool = True,
) -> owner.CodingSessionOwner:
    assert record.generation is not None and record.expected_suffix is not None
    assert record.owner_agent_id is not None
    generation = record.generation
    expected_suffix = record.expected_suffix
    owner_agent_id = record.owner_agent_id
    if supervised:
        supervisor_id = session_id + 100
        record = owner.attach_supervisor(
            record.key,
            generation,
            session_id=supervisor_id,
            session_name=owner.full_session_name(
                owner_agent_id,
                supervisor_id,
                owner.supervisor_suffix(record.key, generation),
            ),
            writer=WRITER,
        )
    return owner.publish_active(
        record.key,
        generation,
        session_id=session_id,
        session_name=owner.full_session_name(owner_agent_id, session_id, expected_suffix),
        writer=WRITER,
    )


def _generation(record: owner.CodingSessionOwner) -> str:
    assert record.generation is not None
    return record.generation


def _live(record: owner.CodingSessionOwner) -> set[str]:
    names = {record.session_name, record.supervisor_session_name}
    return {name for name in names if name is not None}


def _rewrite(record: owner.CodingSessionOwner, **fields: object) -> None:
    path = owner.state_path(record.key, _generation(record))
    payload = cast("dict[str, object]", json.loads(path.read_text()))
    payload.update(fields)
    path.write_text(json.dumps(payload))


def test_a_launch_draws_its_generation_and_stamps_its_time_from_what_it_is_given(
    tmp_path: Path,
) -> None:
    key = _key(tmp_path)

    launched = _launch(key, agent_id=41, takeover=True, rng=seeded_rng(9))

    assert launched.generation == str(new_uuid(seeded_rng(9)))
    assert launched.created_at == NOW
    assert record_codec.host_owner_dir(key) == tmp_path / "cluster" / "coding-session-owners"
    assert owner.state_path(key, _generation(launched)).parent.parent == (
        record_codec.host_owner_dir(key)
    )


def test_a_terminal_record_carries_the_instant_it_was_given(tmp_path: Path) -> None:
    key = _key(tmp_path)
    launched = _launch(key, agent_id=41, takeover=True)
    later = NOW + dt.timedelta(minutes=7)

    assert owner.terminate_generation(
        key,
        _generation(launched),
        reason="explicit-cancel",
        writer=WRITER,
        now=later,
        sessions=_FakeSessions(),
    )

    assert owner.read(key, _generation(launched)).terminalized_at == later


def test_concurrent_launches_each_get_a_generation_of_their_own(tmp_path: Path) -> None:
    key = _key(tmp_path)

    def _attempt(agent_id: int) -> owner.CodingSessionOwner:
        return _launch(key, agent_id=agent_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        launched = list(pool.map(_attempt, range(41, 49)))

    assert len({_generation(record) for record in launched}) == 8
    recorded = owner.list_generations(key)
    assert sorted(map(_generation, recorded)) == sorted(map(_generation, launched))
    assert {record.status for record in recorded} == {"launching"}


def test_a_live_generation_coexists_with_a_new_launch(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41), 3)
    stopped: list[str] = []

    second = _launch(key, agent_id=99, live=_live(active), stopped=stopped)

    assert _generation(second) != _generation(active)
    assert owner.read(key, _generation(active)) == active
    assert stopped == []
    assert len(owner.list_generations(key)) == 2


def test_stale_launching_with_live_session_is_left_alone(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _launch(key, agent_id=41)
    assert first.expected_suffix is not None and first.state_dir is not None
    partial_name = owner.full_session_name(41, 3, first.expected_suffix)
    first.state_dir.mkdir(parents=True)
    stopped: list[str] = []

    _launch(
        key, agent_id=42, now=NOW + dt.timedelta(seconds=61), live={partial_name}, stopped=stopped
    )

    assert owner.read(key, _generation(first)) == first
    assert stopped == []
    assert first.state_dir.exists()


def test_stale_launching_without_live_session_is_reclaimed(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _launch(key, agent_id=41)
    assert first.state_dir is not None
    first.state_dir.mkdir(parents=True)
    stopped: list[str] = []

    second = _launch(key, agent_id=42, now=NOW + dt.timedelta(seconds=61), stopped=stopped)

    assert owner.read(key, _generation(first)).status == "inactive"
    assert [record.generation for record in owner.list_generations(key)] == [second.generation]
    assert stopped == []
    assert not first.state_dir.exists()


def test_publish_active_from_live_stale_generation_succeeds(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _launch(key, agent_id=41)
    assert first.expected_suffix is not None
    partial_name = owner.full_session_name(41, 3, first.expected_suffix)

    _launch(key, agent_id=42, now=NOW + dt.timedelta(seconds=61), live={partial_name})
    active = _publish(first, 3)

    assert active.status == "active"
    assert owner.read(key, _generation(first)) == active


def test_takeover_generation_publishes_without_supervisor(tmp_path: Path) -> None:
    key = _key(tmp_path)

    active = _publish(_launch(key, agent_id=41, takeover=True), 3, supervised=False)

    assert active.status == "active"
    assert active.tasks_file is None and active.work_file is None
    assert active.supervisor_session_id is None and active.supervisor_session_name is None
    assert owner.read(key, _generation(active)) == active


def test_a_live_takeover_generation_is_left_alone(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41, takeover=True), 3, supervised=False)
    stopped: list[str] = []

    _launch(key, agent_id=42, live=_live(active), stopped=stopped, takeover=True)

    assert stopped == []
    assert owner.read(key, _generation(active)) == active


def test_dead_takeover_generation_is_reclaimed_before_the_next_launch(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41, takeover=True), 3, supervised=False)
    assert active.state_dir is not None
    active.state_dir.mkdir(parents=True)
    (active.state_dir / "relay.log").write_text("stale")

    replacement = _launch(key, agent_id=42, takeover=True)

    assert replacement.tasks_file is None and replacement.work_file is None
    assert owner.read(key, _generation(active)).status == "inactive"
    assert not active.state_dir.exists()


def test_a_supervised_generation_without_its_supervisor_is_reclaimed(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41), 3)
    assert active.session_name is not None
    stopped: list[str] = []

    _launch(key, agent_id=42, live={active.session_name}, stopped=stopped)

    assert stopped == [active.session_name]
    assert owner.read(key, _generation(active)).status == "inactive"


def test_takeover_never_attaches_a_supervisor(tmp_path: Path) -> None:
    key = _key(tmp_path)
    record = _launch(key, agent_id=41, takeover=True)

    with pytest.raises(RuntimeError, match="takeover"):
        owner.attach_supervisor(
            key,
            _generation(record),
            session_id=99,
            session_name="meiva-agent-41-shell-99-ignored",
            writer=WRITER,
        )


def test_supervised_active_record_still_requires_its_supervisor(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41), 3)
    _rewrite(active, supervisor_session_id=None, supervisor_session_name=None)

    invalid = owner.read(key, _generation(active))
    assert invalid.status == "invalid"
    assert "supervisor" in (invalid.error or "")


def test_takeover_record_with_a_supervisor_fails_closed(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41, takeover=True), 3, supervised=False)
    _rewrite(active, supervisor_session_id=9, supervisor_session_name="meiva-agent-41-shell-9-x")

    invalid = owner.read(key, _generation(active))
    assert invalid.status == "invalid"
    assert "takeover" in (invalid.error or "")


def test_mixed_file_publication_fails_closed(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41), 3)
    _rewrite(active, work_file=None)

    invalid = owner.read(key, _generation(active))
    assert invalid.status == "invalid"
    assert "published together" in (invalid.error or "")


def test_terminated_owner_is_reclaimed_after_exact_cleanup(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41), 3)
    assert active.session_name is not None and active.state_dir is not None
    active.state_dir.mkdir(parents=True)
    (active.state_dir / "app-server.log").write_text("old")
    stopped: list[str] = []

    replacement = _launch(key, agent_id=99, live=_live(active), stopped=stopped, terminated={41})

    assert replacement.owner_agent_id == 99
    assert stopped == [active.session_name]
    assert not active.state_dir.exists()
    assert owner.read(key, _generation(active)).status == "inactive"


def test_expired_generation_is_reclaimed_before_the_next_launch(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41), 0)
    assert active.session_name is not None and active.state_dir is not None
    assert active.supervisor_session_name is not None
    active.state_dir.mkdir(parents=True)
    live = _live(active)

    _launch(key, agent_id=42, now=NOW + dt.timedelta(hours=2), live=live)

    assert not active.state_dir.exists()
    assert live == {active.supervisor_session_name}


def test_terminal_cleanup_is_generation_scoped_and_removes_state(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41), 0)
    assert active.session_name is not None and active.state_dir is not None
    active.state_dir.mkdir(parents=True)
    stopped: list[str] = []
    sessions = _FakeSessions(_live(active), stopped)

    assert not owner.terminate_generation(
        key,
        "7b1f6a0e-0000-4000-8000-000000000000",
        reason="explicit-cancel",
        writer=WRITER,
        now=NOW,
        sessions=sessions,
    )
    assert stopped == [], "an unrecorded generation must not stop a session"
    assert owner.terminate_generation(
        key,
        _generation(active),
        reason="explicit-cancel",
        writer=WRITER,
        now=NOW + dt.timedelta(minutes=1),
        sessions=sessions,
    )

    terminal = owner.read(key, _generation(active))
    assert terminal.status == "terminal"
    assert terminal.terminal_reason == "explicit-cancel"
    assert stopped == [active.session_name]
    assert not active.state_dir.exists()


def test_a_terminal_generation_is_dropped_by_the_next_launch(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _publish(_launch(key, agent_id=41), 0)
    assert owner.terminate_generation(
        key,
        _generation(first),
        reason="collaboration-handoff",
        writer=WRITER,
        now=NOW,
        sessions=_FakeSessions(_live(first)),
    )

    second = _publish(_launch(key, agent_id=42, now=NOW + dt.timedelta(minutes=1)), 7)

    assert owner.read(key, _generation(first)).status == "inactive"
    assert owner.list_generations(key) == [second]


def test_different_workspaces_have_distinct_records_and_state(tmp_path: Path) -> None:
    first = _launch(_key(tmp_path, "same-name-a/work"), agent_id=41)
    second = _launch(_key(tmp_path, "same-name-b/work"), agent_id=42)

    assert first.key.workspace != second.key.workspace
    assert (
        owner.state_path(first.key, _generation(first)).parent
        != owner.state_path(second.key, _generation(second)).parent
    )
    assert first.state_dir != second.state_dir


def test_same_workspace_in_another_cluster_is_invisible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_key = owner.canonical_key(workspace, tool="codex", cluster=tmp_path / "cluster-a")
    second_key = owner.canonical_key(workspace, tool="codex", cluster=tmp_path / "cluster-b")

    _launch(first_key, agent_id=41)

    assert owner.list_generations(second_key) == []


def test_a_corrupt_record_fails_closed_without_blocking_other_launches(tmp_path: Path) -> None:
    key = _key(tmp_path)
    corrupt = _launch(key, agent_id=41)
    owner.state_path(key, _generation(corrupt)).write_text("{broken")

    second = _launch(key, agent_id=42, now=NOW + dt.timedelta(hours=2))

    assert owner.read(key, _generation(corrupt)).status == "invalid"
    assert owner.read(key, _generation(second)).status == "launching"
    with pytest.raises(owner.InvalidCodingSessionOwnerError):
        owner.terminate_generation(
            key,
            _generation(corrupt),
            reason="explicit-cancel",
            writer=WRITER,
            now=NOW,
            sessions=_FakeSessions(),
        )


def test_misdirected_full_handle_fails_closed_and_is_never_stopped(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_launch(key, agent_id=41), 3)
    assert active.session_name is not None
    _rewrite(active, session_name="meiva-gateway")
    stopped: list[str] = []

    invalid = owner.read(key, _generation(active))
    _launch(key, agent_id=99, live={"meiva-gateway"}, stopped=stopped)

    assert invalid.status == "invalid"
    assert "session_name does not match" in (invalid.error or "")
    assert stopped == []


def test_a_pre_generation_single_slot_record_is_neither_read_nor_removed(tmp_path: Path) -> None:
    key = _key(tmp_path)
    old = _publish(_launch(key, agent_id=41), 3)
    stray = owner.lock_path(key).with_suffix(".json")
    owner.state_path(key, _generation(old)).rename(stray)
    stopped: list[str] = []

    fresh = _launch(key, agent_id=42, stopped=stopped)

    assert stray.exists() and stopped == []
    assert owner.generations(key) == [fresh.generation]


_SOCKET_GENERATION = "be6a5e0a-f271-4301-ad6b-521673bf262f"


@pytest.mark.parametrize(("macos", "root"), [(True, ("private", "tmp")), (False, ("tmp",))])
def test_socket_directory_selects_the_platform_at_call_time(
    tmp_path: Path, macos: bool, root: tuple[str, ...]
) -> None:
    assert owner.default_socket_root(macos=macos) == Path("/").joinpath(*root)

    directory = owner.private_socket_dir(base=tmp_path, macos=macos)

    assert directory == tmp_path / f"meiva-{os.getuid()}"
    info = directory.lstat()
    assert stat.S_ISDIR(info.st_mode) and not directory.is_symlink()
    assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700


@pytest.mark.parametrize(("macos", "limit"), [(True, 104), (False, 108)])
def test_socket_length_limit_selects_the_platform_at_call_time(
    tmp_path: Path, macos: bool, limit: int
) -> None:
    key = _key(tmp_path)
    name_bytes = len("codex-app-server.0123456789ab-be6a5e0a.sock")
    assert owner.sun_path_limit(macos=macos) == limit

    for size in [limit - 1, limit]:
        directory = Path("/" + "x" * (size - name_bytes - 2))
        if size == limit:
            with pytest.raises(owner.CodingSessionSocketError, match=f"at most {limit - 1}"):
                owner.codex_app_server_socket(
                    key, _SOCKET_GENERATION, socket_dir=directory, macos=macos
                )
        else:
            socket = owner.codex_app_server_socket(
                key, _SOCKET_GENERATION, socket_dir=directory, macos=macos
            )
            assert len(os.fsencode(socket)) == size


def test_codex_app_server_socket_is_short_and_generation_scoped(tmp_path: Path) -> None:
    key = _key(tmp_path)
    generation = "be6a5e0a-f271-4301-ad6b-521673bf262f"
    first = owner.codex_app_server_socket(key, generation)
    socket_base = (
        Path("/").joinpath("private", "tmp") if sys.platform == "darwin" else Path("/") / "tmp"
    )
    assert first.parent == socket_base / f"meiva-{os.getuid()}"
    assert first.name.startswith("codex-app-server.")
    assert first.name.endswith("-be6a5e0a.sock")
    assert len(first.name) == len("codex-app-server.") + 12 + 1 + 8 + len(".sock")
    assert owner.codex_app_server_socket(key, generation) == first
    assert owner.codex_app_server_socket(key, "ffffffff-1111-2222-3333-444444444444") != first
    other = owner.codex_app_server_socket(_key(tmp_path, "workspace2"), generation)
    assert other != first


def test_a_long_cluster_home_still_gets_a_socket_under_the_kernel_limit(tmp_path: Path) -> None:
    long_home = tmp_path / ("a-directory-with-a-long-name-" * 4) / "home"
    long_home.mkdir(parents=True)
    workspace = long_home / "workspaces" / "2"
    workspace.mkdir(parents=True)
    key = owner.canonical_key(workspace, tool="codex", cluster=long_home)

    socket = owner.codex_app_server_socket(key, _SOCKET_GENERATION)

    limit = 104 if sys.platform == "darwin" else 108
    assert len(os.fsencode(socket)) < limit
    assert len(os.fsencode(Path(key.cluster) / "run" / socket.name)) >= limit
    info = socket.parent.lstat()
    assert stat.S_ISDIR(info.st_mode) and not socket.parent.is_symlink()
    assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700


def test_a_socket_dir_that_is_not_our_real_directory_is_refused(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / f"meiva-{os.getuid()}").symlink_to(elsewhere)

    with pytest.raises(owner.CodingSessionSocketError, match="not a directory owned by"):
        owner.codex_app_server_socket(_key(tmp_path), _SOCKET_GENERATION, base=tmp_path)


def test_a_socket_path_that_cannot_fit_fails_before_launch(
    tmp_path: Path,
) -> None:
    long_base = tmp_path / ("x" * 90)
    long_base.mkdir()

    with pytest.raises(owner.CodingSessionSocketError, match="bytes; unix sockets"):
        owner.codex_app_server_socket(_key(tmp_path), _SOCKET_GENERATION, base=long_base)


@pytest.mark.parametrize("status", ["inactive", "invalid"])
def test_observation_outcomes_cannot_be_written(tmp_path: Path, status: str) -> None:
    key = _key(tmp_path)
    launched = _launch(key, agent_id=7)
    projected = replace(launched, status=record_codec.CodingSessionStatus(status))
    with pytest.raises(ValueError, match="only lifecycle states"):
        record_codec.write_unlocked(projected, writer=WRITER)
    assert (
        owner.read(key, _generation(launched)).status is record_codec.CodingSessionStatus.LAUNCHING
    )


def test_write_unlocked_propagates_a_real_target_directory_failure(tmp_path: Path) -> None:
    key = _key(tmp_path)
    launched = _launch(key, agent_id=7)
    path = owner.state_path(key, _generation(launched))
    path.unlink()
    path.mkdir()
    retained = path / "existing"
    retained.write_bytes(b"old")

    with pytest.raises(OSError):
        record_codec.write_unlocked(launched, writer=WRITER)
    assert retained.read_bytes() == b"old"


def test_write_unlocked_survives_directory_sync_failure_and_cleans_temps(
    tmp_path: Path,
) -> None:
    key = _key(tmp_path)
    launched = _launch(key, agent_id=7)
    path = owner.state_path(key, _generation(launched))
    path.write_bytes(b"old")

    synced: list[str] = []

    def sync_files_only(fd: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        synced.append("directory" if is_directory else "file")
        if is_directory:
            raise OSError("directory sync failure")
        os.fsync(fd)

    record_codec.write_unlocked(
        launched,
        writer=AtomicWriter(replace(SYSTEM_FS, fsync=sync_files_only)),
    )
    assert synced == ["file", "directory"]
    assert owner.read(key, _generation(launched)) == launched
    assert sorted(path.parent.iterdir()) == [path]
    assert path.stat().st_mode & 0o777 == 0o600


def test_owner_snapshot_rejects_unknown_status(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        record_codec.CodingSessionOwner(key=_key(tmp_path), status=cast(Any, "unexpected"))


@pytest.mark.parametrize("status", list(record_codec.CodingSessionStatus))
def test_valid_legacy_strings_normalize_without_wire_changes(
    tmp_path: Path, status: record_codec.CodingSessionStatus
) -> None:
    snapshot = record_codec.CodingSessionOwner(key=_key(tmp_path), status=cast(Any, status.value))
    assert snapshot.status is status
    assert json.loads(json.dumps(record_codec.owner_payload(snapshot)))["status"] == status.value


# meiva:note
# A `~` that reaches the record means the entry that read the operator's home skipped its step: the
# key refuses it instead of expanding it against the process's own user database, so a record can
# never name a home nobody chose for it. Each side of the key is pinned separately.
def test_canonical_key_refuses_a_tilde_instead_of_expanding_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cluster = tmp_path / "cluster"
    cluster.mkdir()

    with pytest.raises(ValueError, match="workspace '~/workspace' starts with ~"):
        owner.canonical_key("~/workspace", tool="codex", cluster=cluster)
    with pytest.raises(ValueError, match="cluster '~/cluster' starts with ~"):
        owner.canonical_key(workspace, tool="codex", cluster="~/cluster")


# meiva:note
# The generation stores the task and work files it is handed for the same reason: a tilde here is a
# step the launcher skipped, and expanding it in the record would answer with whichever home the
# process happens to run under.
def test_launch_generation_refuses_a_tilde_instead_of_expanding_it(tmp_path: Path) -> None:
    key = _key(tmp_path)
    workspace = Path(key.workspace)

    def launch(*, tasks_file: Path, work_file: Path) -> owner.CodingSessionOwner:
        return owner.launch_generation(
            key,
            owner_agent_id=1,
            tasks_file=tasks_file,
            work_file=work_file,
            ttl_seconds=3600,
            writer=WRITER,
            now=NOW,
            rng=RNG,
            sessions=_FakeSessions(),
        )

    with pytest.raises(ValueError, match=r"~/tasks\.md starts with ~"):
        launch(tasks_file=Path("~/tasks.md"), work_file=workspace / "work.md")
    with pytest.raises(ValueError, match=r"~/work\.md starts with ~"):
        launch(tasks_file=workspace / "tasks.md", work_file=Path("~/work.md"))

    # meiva:note
    # The refusal happens before anything is published: a record that named a home nobody chose
    # would be worse than no record, so the launch leaves none behind.
    assert not list(tmp_path.rglob("*.json"))
