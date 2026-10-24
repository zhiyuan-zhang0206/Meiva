import psutil
import pytest

from base.native_process.ownership import LiveReading, OwnedProcess

_RECORDED = OwnedProcess(4242, 1.0, 777)


def _reading(
    *, ticks: int | None = 777, exists: bool = True, status: str = psutil.STATUS_RUNNING
) -> LiveReading:
    return LiveReading(ticks=ticks, pid_exists=exists, status=status, identity=None)


def test_matching_ticks_on_a_running_process_are_live() -> None:
    assert _RECORDED.judge_live(_reading())


def test_no_process_at_the_pid_is_not_live() -> None:
    assert not _RECORDED.judge_live(None)


def test_a_reused_pid_with_other_ticks_is_not_live() -> None:
    assert not _RECORDED.judge_live(_reading(ticks=778))


@pytest.mark.parametrize("status", [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD])
def test_a_zombie_or_dead_process_is_not_live(status: str) -> None:
    assert not _RECORDED.judge_live(_reading(status=status))


def test_a_pid_that_vanished_between_the_reads_converges_as_gone() -> None:
    assert not _RECORDED.judge_live(_reading(ticks=None, exists=False))


def test_a_pid_that_exists_but_cannot_be_read_stays_an_error() -> None:
    with pytest.raises(RuntimeError, match="cannot verify process identity"):
        _RECORDED.judge_live(_reading(ticks=None, exists=True))


@pytest.mark.requires_host("darwin")
def test_a_record_without_ticks_is_judged_by_the_identity_read_now() -> None:
    record = OwnedProcess(4242, 5.0, None)
    same = LiveReading(ticks=None, pid_exists=True, status="running", identity=record)
    other = LiveReading(
        ticks=None, pid_exists=True, status="running", identity=OwnedProcess(4242, 6.0, None)
    )

    assert record.judge_live(same)
    assert not record.judge_live(other)


@pytest.mark.requires_host("linux")
def test_a_linux_record_without_ticks_is_never_live() -> None:
    record = OwnedProcess(4242, 5.0, None)
    same = LiveReading(ticks=None, pid_exists=True, status="running", identity=record)

    assert not record.judge_live(same)
