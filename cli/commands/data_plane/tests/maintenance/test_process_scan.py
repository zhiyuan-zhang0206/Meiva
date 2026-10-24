# meiva:note
# Which data-plane processes the maintenance stop has not recorded, judged from what the
# process table showed: this home's and not recorded, another home's, or unreadable.

from __future__ import annotations

from collections.abc import Mapping

from cli.commands.data_plane.maintenance.process_scan import (
    ScanVerdict,
    SeenProcess,
    judge,
    refusal,
    service_of,
)

_HOMES: Mapping[str, str] = {
    "postgres": "/h/pg",
    "redis": "/h/redis",
    "pgbouncer": "/h/pgbouncer/pgbouncer.ini",
}


def _process(
    pid: int,
    name: str,
    locator: str | None,
    *,
    other_user: bool = False,
    exiting: bool = False,
) -> SeenProcess:
    return SeenProcess(pid, name, other_user=other_user, exiting=exiting, locator=locator)


def _judge(*seen: SeenProcess, recorded: Mapping[str, frozenset[int]] | None = None) -> ScanVerdict:
    return judge(seen, homes=_HOMES, recorded=recorded or {})


def test_every_data_plane_process_name_maps_to_its_service() -> None:
    assert [
        service_of(name) for name in ("postgres", "postmaster", "redis-server", "pgbouncer")
    ] == [
        "postgres",
        "postgres",
        "redis",
        "pgbouncer",
    ]
    assert service_of("nginx") is None


def test_a_process_of_this_home_that_nothing_recorded_is_unrecorded() -> None:
    stray = _process(10, "postgres", "/h/pg")

    assert _judge(stray) == ScanVerdict(unrecorded=(stray,), undeterminable=())


def test_a_recorded_process_and_its_workers_are_accounted_for() -> None:
    postmaster = _process(10, "postgres", "/h/pg")
    worker = _process(11, "postgres", "/h/pg")

    verdict = _judge(postmaster, worker, recorded={"postgres": frozenset({10, 11})})

    assert verdict == ScanVerdict(unrecorded=(), undeterminable=())


def test_a_replacement_of_a_recorded_pid_is_unrecorded() -> None:
    replacement = _process(12, "postgres", "/h/pg")

    verdict = _judge(replacement, recorded={"postgres": frozenset({10, 11})})

    assert verdict.unrecorded == (replacement,)


# meiva:note
# A co-located cluster's Postgres, Redis and pooler share the process names; they live in
# another directory and are none of this stop's business.
def test_processes_of_another_home_are_not_unrecorded() -> None:
    others = (
        _process(20, "postgres", "/other/pg"),
        _process(21, "redis-server", "/other/redis"),
        _process(22, "pgbouncer", "/other/pgbouncer/pgbouncer.ini"),
        _process(23, "pgbouncer", ""),
    )

    assert _judge(*others) == ScanVerdict(unrecorded=(), undeterminable=())


def test_a_process_of_another_user_or_one_that_is_exiting_is_not_a_candidate() -> None:
    verdict = _judge(
        _process(30, "postgres", "/h/pg", other_user=True),
        _process(31, "redis-server", "/h/redis", exiting=True),
        _process(32, "bash", "/h/pg"),
    )

    assert verdict == ScanVerdict(unrecorded=(), undeterminable=())


# meiva:note
# A same-user process whose attributing path cannot be read is neither this home's nor another's
# as far as the scan can prove, so it is reported as unreadable instead of raising or passing.
def test_a_process_whose_directory_cannot_be_read_is_undeterminable() -> None:
    hidden = _process(40, "postgres", None)

    assert _judge(hidden) == ScanVerdict(unrecorded=(), undeterminable=(hidden,))


def test_an_unreadable_process_that_is_recorded_needs_no_attribution() -> None:
    hidden = _process(40, "postgres", None)

    assert _judge(hidden, recorded={"postgres": frozenset({40})}) == ScanVerdict((), ())


def test_a_pid_recorded_for_another_service_does_not_account_for_this_one() -> None:
    redis = _process(50, "redis-server", "/h/redis")

    assert _judge(redis, recorded={"postgres": frozenset({50})}).unrecorded == (redis,)


def test_nothing_unrecorded_and_nothing_unreadable_refuses_nothing() -> None:
    assert refusal(ScanVerdict((), ())) is None


def test_an_unrecorded_process_is_refused_with_its_service_and_pid() -> None:
    verdict = _judge(_process(10, "postgres", "/h/pg"), _process(11, "redis-server", "/h/redis"))

    message = refusal(verdict)

    assert message is not None
    assert message.startswith("unrecorded or replacement data-plane process prevents")
    assert "postgres pid 10" in message and "redis pid 11" in message


# meiva:note
# The scan exists to prove that nothing unrecorded runs. A process it cannot attribute is a
# refusal that names the pid and what to do, because passing it would let the one process the
# scan cannot see through slip by.
def test_an_undeterminable_process_is_refused_with_its_pid_and_what_to_do() -> None:
    message = refusal(_judge(_process(40, "postgres", None)))

    assert message is not None
    assert "cannot tell whether a data-plane process belongs to this home" in message
    assert "postgres pid 40" in message
    assert "unreadable" in message and "End it as its owner" in message


def test_an_unrecorded_process_is_named_before_an_unreadable_one() -> None:
    verdict = _judge(_process(40, "postgres", None), _process(10, "postgres", "/h/pg"))

    message = refusal(verdict)

    assert message is not None and message.startswith("unrecorded or replacement")
    assert "pid 40" not in message
