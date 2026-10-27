# meiva:note
# The supervision verdicts of `watch_work.py`, over values, and the canonical supervisor against a real owner record on disk and a real gateway listener on port 0.

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from base.clock.tests.fakes import system_clock
from base.clock.tests.seeded_rng import seeded_rng
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.sessions import coding_session_owner as co
from base.sessions.coding_session_owner_record import CodingSessionOwner, CodingSessionStatus
from meiva.tests.fixtures.gateway_wire import GatewayStub, GatewayWire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.skills.tests.skill_script import load_skill_script

ww = load_skill_script("platform", "meiva-guide", "external-agents", "scripts", "watch_work.py")
_WRITER = AtomicWriter(SYSTEM_FS)
_OWNER = 41
_CADENCE = ww.Cadence(poll_seconds=600.0, stall_seconds=600.0, heartbeat_seconds=480.0)


def _beat(
    status: str | None = "WORKING",
    *,
    mtime: float | None = 100.0,
    saw: bool = True,
    total: float = 10.0,
    change: float = 1.0,
    wake: float = 1.0,
) -> Any:
    return ww.Beat(status, mtime, saw, total, change, wake)


def _first(path: str = "w.md") -> tuple[Any, str]:
    return ww.Memory(first_poll=True, last_actionable_mtime=None), path


def test_parse_status_reads_the_first_token_of_the_first_status_line() -> None:
    assert ww.parse_status("# notes\nSTATUS: NEED_INPUT\nSTATUS: DONE\n") == "NEED_INPUT"
    assert ww.parse_status("STATUS:DONE") == "DONE"
    assert ww.parse_status("status: done\nnothing here") is None
    assert ww.parse_status("") is None


def test_read_status_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert ww.read_status(str(tmp_path / "gone.md")) is None


@pytest.mark.parametrize("status", ["DONE", "NEED_INPUT", "HANDOFF"])
def test_a_status_already_there_when_armed_is_the_baseline_not_a_wake(status: str) -> None:
    memory, path = _first()

    wake, after = ww.generic_verdict(_beat(status), memory, path, _CADENCE)

    assert wake is None
    assert after == ww.Memory(first_poll=False, last_actionable_mtime=100.0)


def test_working_at_arming_leaves_the_baseline_unset() -> None:
    memory, path = _first()

    wake, after = ww.generic_verdict(_beat("WORKING"), memory, path, _CADENCE)

    assert wake is None
    assert after == ww.Memory(first_poll=False, last_actionable_mtime=None)


def test_an_actionable_status_with_a_new_mtime_wakes_once_named() -> None:
    memory = ww.Memory(first_poll=False, last_actionable_mtime=100.0)

    wake, _ = ww.generic_verdict(_beat("DONE", mtime=101.0), memory, "w.md", _CADENCE)

    assert wake == ww.Wake("coding agent reported STATUS: DONE in w.md -- read that file")


def test_an_unchanged_actionable_status_heartbeats_only_after_the_interval() -> None:
    memory = ww.Memory(first_poll=False, last_actionable_mtime=100.0)

    quiet, _ = ww.generic_verdict(_beat("DONE", wake=480.0), memory, "w.md", _CADENCE)
    beat, _ = ww.generic_verdict(_beat("DONE", wake=480.5), memory, "w.md", _CADENCE)

    assert quiet is None
    assert beat == ww.Wake(
        "coding agent heartbeat: STATUS is 'DONE' in w.md (unchanged since arming)"
    )


@pytest.mark.parametrize("status", ["WORKING", None])
def test_a_stalled_working_file_wakes_with_the_stall_window(status: str | None) -> None:
    memory = ww.Memory(first_poll=False, last_actionable_mtime=None)

    wake, _ = ww.generic_verdict(_beat(status, change=601.0), memory, "w.md", _CADENCE)

    assert wake == ww.Wake(
        "coding agent has been WORKING with no change to w.md for over 600.0s -- capture its screen"
    )


@pytest.mark.parametrize(("status", "label"), [("WORKING", "WORKING"), (None, "MISSING")])
def test_a_working_file_that_keeps_changing_heartbeats_with_the_status_it_shows(
    status: str | None, label: str
) -> None:
    memory = ww.Memory(first_poll=False, last_actionable_mtime=None)

    wake, _ = ww.generic_verdict(_beat(status, change=1.0, wake=481.0), memory, "w.md", _CADENCE)

    assert wake == ww.Wake(f"coding agent heartbeat: STATUS is {label!r} in w.md")


def test_a_working_file_inside_every_window_stays_quiet() -> None:
    memory = ww.Memory(first_poll=False, last_actionable_mtime=None)

    wake, _ = ww.generic_verdict(
        _beat("WORKING", change=599.0, wake=479.0), memory, "w.md", _CADENCE
    )

    assert wake is None


def test_an_unrecognized_status_wakes_naming_it() -> None:
    memory = ww.Memory(first_poll=False, last_actionable_mtime=None)

    wake, _ = ww.generic_verdict(_beat("PAUSED"), memory, "w.md", _CADENCE)

    assert wake == ww.Wake("coding agent reported unknown STATUS: 'PAUSED' in w.md")


def test_a_deleted_work_file_wakes_before_the_hard_limit_does() -> None:
    memory = ww.Memory(first_poll=False, last_actionable_mtime=None)
    gone = _beat(None, mtime=None, saw=True, total=99999.0)

    wake, _ = ww.generic_verdict(gone, memory, "w.md", _CADENCE)

    assert wake == ww.Wake(
        "work file deleted: w.md -- the coding agent may have removed its workspace"
    )


def test_a_file_that_never_existed_is_not_a_deletion() -> None:
    memory = ww.Memory(first_poll=False, last_actionable_mtime=None)

    wake, _ = ww.generic_verdict(_beat(None, mtime=None, saw=False), memory, "w.md", _CADENCE)

    assert wake is None


def test_the_hard_limit_wakes_the_launcher_with_the_limit_in_the_message() -> None:
    memory = ww.Memory(first_poll=False, last_actionable_mtime=None)
    cadence = ww.Cadence(hard_limit_seconds=7200)

    wake, _ = ww.generic_verdict(_beat("WORKING", total=7201.0), memory, "w.md", cadence)

    assert wake == ww.Wake("hard limit reached while polling w.md for over 7200s")


def test_the_default_cadence_is_the_documented_constants() -> None:
    assert (
        ww.Cadence(ww.POLL_SECONDS, ww.STALL_SECONDS, ww.HEARTBEAT_SECONDS, ww.HARD_LIMIT_SECONDS)
        == ww.DEFAULT_CADENCE
    )
    assert (ww.POLL_SECONDS, ww.STALL_SECONDS, ww.HEARTBEAT_SECONDS) == (60, 600, 480)
    assert ww.HARD_LIMIT_SECONDS == 7200


def _facts(
    *,
    created_at: float | None = 50.0,
    terminated: bool = False,
    crashed: bool = False,
    expired: bool = False,
) -> Any:
    return ww.OwnerFacts(created_at, terminated, crashed, expired)


@pytest.mark.parametrize(
    ("status", "reason"),
    [("DONE", "collaboration-done"), ("HANDOFF", "collaboration-handoff")],
)
def test_a_current_done_or_handoff_terminalizes_the_generation(status: str, reason: str) -> None:
    verdict = ww.canonical_verdict(_beat(status, mtime=60.0), _facts(), None, "w.md", _CADENCE)

    assert verdict == ww.Terminalize(reason)


def test_a_done_written_before_the_generation_began_is_not_current() -> None:
    verdict = ww.canonical_verdict(_beat("DONE", mtime=40.0), _facts(), None, "w.md", _CADENCE)

    assert verdict == ww.Quiet()


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        ({"terminated": True}, "owner-terminated"),
        ({"crashed": True}, "session-crashed"),
        ({"expired": True}, "expired"),
    ],
)
def test_the_end_of_the_owner_the_session_or_the_lease_terminalizes(
    facts: dict[str, bool], reason: str
) -> None:
    verdict = ww.canonical_verdict(_beat("WORKING"), _facts(**facts), None, "w.md", _CADENCE)

    assert verdict == ww.Terminalize(reason)


def test_the_reasons_keep_their_order() -> None:
    everything = _facts(terminated=True, crashed=True, expired=True)

    verdict = ww.canonical_verdict(_beat("DONE", mtime=60.0), everything, None, "w.md", _CADENCE)
    without_done = ww.canonical_verdict(_beat("WORKING"), everything, None, "w.md", _CADENCE)

    assert verdict == ww.Terminalize("collaboration-done")
    assert without_done == ww.Terminalize("owner-terminated")


def test_a_deleted_work_file_terminalizes_but_only_if_it_was_ever_seen() -> None:
    seen = ww.canonical_verdict(_beat(None, mtime=None, saw=True), _facts(), None, "w.md", _CADENCE)
    never = ww.canonical_verdict(
        _beat(None, mtime=None, saw=False), _facts(), None, "w.md", _CADENCE
    )

    assert seen == ww.Terminalize("work-file-deleted")
    assert never == ww.Quiet()


def test_the_generic_hard_limit_never_shortens_a_canonical_lease() -> None:
    verdict = ww.canonical_verdict(_beat("WORKING", total=10**9), _facts(), None, "w.md", _CADENCE)

    assert verdict == ww.Quiet()


def test_a_current_need_input_wakes_once_per_modification() -> None:
    first = ww.canonical_verdict(_beat("NEED_INPUT", mtime=60.0), _facts(), None, "w.md", _CADENCE)
    again = ww.canonical_verdict(_beat("NEED_INPUT", mtime=60.0), _facts(), 60.0, "w.md", _CADENCE)
    rewritten = ww.canonical_verdict(
        _beat("NEED_INPUT", mtime=61.0), _facts(), 60.0, "w.md", _CADENCE
    )

    assert first == ww.NeedInput(
        "coding agent reported STATUS: NEED_INPUT in w.md -- read the file and reply"
    )
    assert again == ww.Quiet()
    assert rewritten == first


def test_a_stall_needs_both_the_stall_window_and_the_heartbeat_interval() -> None:
    both = ww.canonical_verdict(
        _beat("WORKING", change=700.0, wake=500.0), _facts(), None, "w.md", _CADENCE
    )
    no_heartbeat_yet = ww.canonical_verdict(
        _beat("WORKING", change=700.0, wake=100.0), _facts(), None, "w.md", _CADENCE
    )
    not_stalled = ww.canonical_verdict(
        _beat("WORKING", change=100.0, wake=500.0), _facts(), None, "w.md", _CADENCE
    )

    assert both == ww.Stalled("coding agent has made no work-file change for 700s: w.md")
    assert no_heartbeat_yet == ww.Quiet()
    assert not_stalled == ww.Quiet()


def test_only_a_terminated_or_unknown_owner_is_gone() -> None:
    gone = {read: ww.owner_terminated(read) for read in ww.OwnerRead}

    assert gone == {
        ww.OwnerRead.ALIVE: False,
        ww.OwnerRead.TERMINATED: True,
        ww.OwnerRead.NOT_FOUND: True,
        ww.OwnerRead.UNAVAILABLE: False,
    }


def test_a_gateway_outage_is_reported_once_and_its_end_once() -> None:
    down, first = ww.gateway_edge(False, ww.OwnerRead.UNAVAILABLE, 41, "boom")
    still_down, second = ww.gateway_edge(down, ww.OwnerRead.UNAVAILABLE, 41, "boom")
    up, recovered = ww.gateway_edge(still_down, ww.OwnerRead.ALIVE, 41, "")
    quiet_up, nothing = ww.gateway_edge(up, ww.OwnerRead.ALIVE, 41, "")

    assert (down, still_down, up, quiet_up) == (True, True, False, False)
    assert first == "gateway unavailable checking owner agent 41; treating it as alive: boom"
    assert second is None
    assert recovered == "gateway reachable again"
    assert nothing is None


@pytest.mark.parametrize(
    ("status", "stale", "named", "alive", "crashed"),
    [
        (CodingSessionStatus.LAUNCHING, True, False, None, True),
        (CodingSessionStatus.LAUNCHING, False, False, None, False),
        (CodingSessionStatus.ACTIVE, False, True, False, True),
        (CodingSessionStatus.ACTIVE, False, True, True, False),
        (CodingSessionStatus.ACTIVE, False, False, None, False),
        (CodingSessionStatus.TERMINAL, True, True, False, False),
    ],
)
def test_session_crashed_by_the_state_of_the_generation(
    status: CodingSessionStatus, stale: bool, named: bool, alive: bool | None, crashed: bool
) -> None:
    result = ww.session_crashed(
        status, launch_stale=stale, has_session_name=named, session_alive=alive
    )

    assert result is crashed


@pytest.mark.parametrize(
    ("matches", "status", "over"),
    [
        (True, CodingSessionStatus.LAUNCHING, False),
        (True, CodingSessionStatus.ACTIVE, False),
        (True, CodingSessionStatus.TERMINAL, True),
        (True, CodingSessionStatus.INACTIVE, True),
        (True, CodingSessionStatus.INVALID, True),
        (False, CodingSessionStatus.ACTIVE, True),
    ],
)
def test_a_generation_is_over_when_replaced_or_no_longer_live(
    matches: bool, status: CodingSessionStatus, over: bool
) -> None:
    assert ww.generation_over(matches, status) is over


@pytest.fixture
def gateway(sdk_environment: object, gateway_stub: GatewayStub) -> Iterator[GatewayWire]:
    """The owner agent 41 as the supervisor sees the cluster: a listener on port 0 that answers
    its status read as idling and accepts its system notes."""
    with GatewayWire.open(gateway_stub, retries=2) as wire:
        pin_agent(_OWNER, clients=wire.context.clients)
        wire.stub.reply("GET", f"/api/agents/{_OWNER}", {"status": "idling"})
        wire.stub.reply("POST", f"/api/agents/{_OWNER}/system-note", {"inbound_id": 3}, status=201)
        yield wire


# meiva:note
# The shell sessions of the key's cluster, as the watcher reaches them: no PTY service listens under a test's cluster, so none is live there.
def _cluster_sessions(key: co.CodingSessionKey) -> Any:
    return ww.cluster_sessions(key)


def test_the_watcher_reads_the_sessions_of_the_cluster_its_record_lives_in(tmp_path: Path) -> None:
    key = co.canonical_key(tmp_path / "workspace", tool="codex", cluster=tmp_path / "cluster")

    assert _cluster_sessions(key).session_log_path("meiva-x") == (
        tmp_path / "cluster" / "logs" / "meiva-x.out.log"
    )


def _launch(
    tmp_path: Path, *, ttl: float = 3600.0
) -> tuple[co.CodingSessionKey, CodingSessionOwner]:
    key = co.canonical_key(tmp_path / "workspace", tool="codex", cluster=tmp_path / "cluster")
    owner = co.launch_generation(
        key,
        owner_agent_id=_OWNER,
        tasks_file=tmp_path / "tasks.md",
        work_file=tmp_path / "work.md",
        ttl_seconds=ttl,
        writer=_WRITER,
        now=system_clock().now(),
        rng=seeded_rng(),
        sessions=_cluster_sessions(key),
    )
    return key, owner


# meiva:note
# Publish a handle for a PTY that does not exist, as after the coding session died.
def _activate(owner: CodingSessionOwner) -> CodingSessionOwner:
    generation, suffix = owner.generation, owner.expected_suffix
    assert generation is not None and suffix is not None
    supervisor = co.full_session_name(_OWNER, 2, co.supervisor_suffix(owner.key, generation))
    co.attach_supervisor(
        owner.key, generation, session_id=2, session_name=supervisor, writer=_WRITER
    )
    return co.publish_active(
        owner.key,
        generation,
        session_id=1,
        session_name=co.full_session_name(_OWNER, 1, suffix),
        writer=_WRITER,
    )


# meiva:note
# Write the work file with a modification time after the generation began.
def _current(work: Path, text: str) -> None:
    work.write_text(text)
    mtime = time.time() + 1
    os.utime(work, (mtime, mtime))


# meiva:note
# Run the supervisor to the end of its generation. `after` runs a step on another thread after a delay; a safety stop ends a supervisor that would otherwise outlive a failing test.
def _supervise(
    key: co.CodingSessionKey,
    owner: CodingSessionOwner,
    work: Path,
    *,
    cadence: Any | None = None,
    after: tuple[float, Any] | None = None,
) -> None:
    generation = owner.generation
    assert generation is not None

    def stop() -> None:
        co.terminate_generation(
            key,
            generation,
            reason="test-over",
            writer=_WRITER,
            now=system_clock().now(),
            sessions=_cluster_sessions(key),
        )

    timers = [threading.Timer(15.0, stop)]
    if after is not None:
        timers.append(threading.Timer(after[0], after[1]))
    for timer in timers:
        timer.start()
    try:
        ww.watch(
            str(work),
            cluster=key.cluster,
            workspace=key.workspace,
            generation=generation,
            owner_agent_id=_OWNER,
            cadence=cadence if cadence is not None else ww.Cadence(poll_seconds=0.01),
        )
    finally:
        for timer in timers:
            timer.cancel()
            timer.join()


def _notes(wire: GatewayWire) -> list[str]:
    return [
        sent.body["content"]
        for sent in wire.sent
        if sent.path == f"/api/agents/{_OWNER}/system-note"
    ]


@pytest.mark.parametrize(
    ("status", "reason"),
    [("DONE", "collaboration-done"), ("HANDOFF", "collaboration-handoff")],
)
def test_a_finished_collaboration_terminalizes_its_generation_and_tells_the_owner(
    gateway: GatewayWire, tmp_path: Path, status: str, reason: str
) -> None:
    key, owner = _launch(tmp_path)
    work = tmp_path / "work.md"
    _current(work, f"STATUS: {status}\n")

    _supervise(key, owner, work)

    assert owner.generation is not None
    record = co.read(key, owner.generation)
    assert (record.status, record.terminal_reason) == (CodingSessionStatus.TERMINAL, reason)
    assert _notes(gateway) == [f"Codex generation {owner.generation} terminalized ({reason})."]


def test_a_dead_coding_session_terminalizes_the_active_generation(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    key, launching = _launch(tmp_path)
    owner = _activate(launching)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")

    _supervise(key, owner, work)

    assert owner.generation is not None
    record = co.read(key, owner.generation)
    assert (record.status, record.terminal_reason) == (
        CodingSessionStatus.TERMINAL,
        "session-crashed",
    )
    assert len(_notes(gateway)) == 1


def test_an_owner_that_terminated_ends_the_generation_without_a_note(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    gateway.stub.reply("GET", f"/api/agents/{_OWNER}", {"status": "terminated"})
    key, owner = _launch(tmp_path)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")

    _supervise(key, owner, work)

    assert owner.generation is not None
    assert co.read(key, owner.generation).terminal_reason == "owner-terminated"
    assert _notes(gateway) == []


def test_an_owner_the_gateway_does_not_know_counts_as_terminated(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    gateway.stub.reply(
        "GET",
        f"/api/agents/{_OWNER}",
        {"reason": "agent_not_found", "detail": "no such agent"},
        status=404,
    )
    key, owner = _launch(tmp_path)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")

    _supervise(key, owner, work)

    assert owner.generation is not None
    assert co.read(key, owner.generation).terminal_reason == "owner-terminated"


def test_an_expired_lease_terminalizes_the_generation(gateway: GatewayWire, tmp_path: Path) -> None:
    key, owner = _launch(tmp_path, ttl=0.05)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")

    _supervise(key, owner, work)

    assert owner.generation is not None
    assert co.read(key, owner.generation).terminal_reason == "expired"


def test_a_work_file_removed_after_it_was_seen_terminalizes_the_generation(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    key, owner = _launch(tmp_path)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")

    _supervise(key, owner, work, after=(0.1, work.unlink))

    assert owner.generation is not None
    assert co.read(key, owner.generation).terminal_reason == "work-file-deleted"


# meiva:note
# The gateway cannot prove an owner dead, so supervision continues; the outage and its end are each printed once however many polls they span.
def test_an_unreachable_gateway_is_survived_and_reported_once_each_way(
    gateway: GatewayWire, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway.stub.reply("GET", f"/api/agents/{_OWNER}", {}, status=503)
    key, owner = _launch(tmp_path)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")
    generation = owner.generation
    assert generation is not None

    def recover() -> None:
        gateway.stub.reply("GET", f"/api/agents/{_OWNER}", {"status": "idling"})

    def finish() -> None:
        co.terminate_generation(
            key,
            generation,
            reason="test-over",
            writer=_WRITER,
            now=system_clock().now(),
            sessions=_cluster_sessions(key),
        )

    recovering = threading.Timer(0.25, recover)
    finishing = threading.Timer(0.6, finish)
    recovering.start()
    finishing.start()
    try:
        _supervise(key, owner, work)
    finally:
        recovering.cancel()
        finishing.cancel()
        recovering.join()
        finishing.join()

    lines = capsys.readouterr().out.splitlines()
    assert sum("gateway unavailable checking owner agent 41" in line for line in lines) == 1
    assert lines.count("gateway reachable again") == 1
    assert co.read(key, generation).terminal_reason == "test-over"


# meiva:note
# A 4xx other than "no such agent" is a defect, not an outage: it ends the supervisor loudly instead of being read as a live owner.
def test_a_refusal_that_is_not_an_outage_is_not_swallowed(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    gateway.stub.reply("GET", f"/api/agents/{_OWNER}", {}, status=401)
    key, owner = _launch(tmp_path)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")

    with pytest.raises(httpx.HTTPStatusError) as refused:
        _supervise(key, owner, work)

    assert refused.value.response.status_code == 401


def test_a_stalled_canonical_run_wakes_the_owner_at_each_heartbeat(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    key, owner = _launch(tmp_path)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")
    generation = owner.generation
    assert generation is not None

    def finish() -> None:
        co.terminate_generation(
            key,
            generation,
            reason="test-over",
            writer=_WRITER,
            now=system_clock().now(),
            sessions=_cluster_sessions(key),
        )

    cadence = ww.Cadence(poll_seconds=0.01, stall_seconds=0.05, heartbeat_seconds=0.1)
    _supervise(key, owner, work, cadence=cadence, after=(0.45, finish))

    notes = _notes(gateway)
    assert 2 <= len(notes) <= 5
    assert all(note.startswith("coding agent has made no work-file change for ") for note in notes)
