import faulthandler
import sys
import threading
from pathlib import Path
from typing import TextIO

import pytest
import yaml

from scripts.ci.collection_tracebacks import periodic_tracebacks

ROOT = Path(__file__).resolve().parents[3]


def assert_worker_stopped() -> None:
    assert not any(t.name == "collection-tracebacks" for t in threading.enumerate())


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_invalid_interval_starts_no_worker(tmp_path: Path, interval: float) -> None:
    with (
        (tmp_path / "stacks.log").open("w") as stacks,
        pytest.raises(ValueError, match="finite and positive"),
        periodic_tracebacks(stacks, interval=interval, dump_traceback=faulthandler.dump_traceback),
    ):
        pytest.fail("Invalid interval reached collection")
    assert_worker_stopped()


def test_early_completion_cancels_wait_without_dumping(tmp_path: Path) -> None:
    output = tmp_path / "stacks.log"
    enabled = faulthandler.is_enabled()
    with (
        output.open("w") as stacks,
        periodic_tracebacks(stacks, interval=600, dump_traceback=faulthandler.dump_traceback),
    ):
        pass
    assert output.read_text() == ""
    assert faulthandler.is_enabled() is enabled
    assert_worker_stopped()


def test_repeating_dumps_stop_before_file_closes(tmp_path: Path) -> None:
    twice = threading.Event()
    calls: list[tuple[int, bool]] = []

    def dump(*, file: TextIO, all_threads: bool) -> None:
        assert not file.closed
        calls.append((threading.get_ident(), all_threads))
        if len(calls) >= 2:
            twice.set()

    with (tmp_path / "stacks.log").open("w") as stacks:
        with periodic_tracebacks(stacks, interval=0.001, dump_traceback=dump):
            assert twice.wait(2)
        count = len(calls)
        assert_worker_stopped()
    assert len(calls) == count
    assert all(tid != threading.get_ident() and all_threads for tid, all_threads in calls)


def test_diagnostic_failure_reaches_owner(tmp_path: Path) -> None:
    attempted = threading.Event()

    def dump(*, file: TextIO, all_threads: bool) -> None:
        attempted.set()
        raise OSError("Diagnostic output unavailable")

    with (
        (tmp_path / "stacks.log").open("w") as stacks,
        pytest.raises(RuntimeError, match="diagnostic failed") as failure,
        periodic_tracebacks(stacks, interval=0.001, dump_traceback=dump),
    ):
        assert attempted.wait(2)
    assert isinstance(failure.value.__cause__, OSError)
    assert_worker_stopped()


# meiva:note
# `timeout` only catches a child that hangs: a churn test parks a frame and waits for a dump, so a
# stuck child costs the whole budget instead of the ~0.3 s the child normally needs.
# meiva:link scripts/ci/collection_tracebacks.py
def run_isolated(pytester: pytest.Pytester, code: str, *, timeout: float = 3) -> pytest.RunResult:
    assert Path.cwd() == pytester.path
    return pytester.run(
        sys.executable,
        "-I",
        "-S",
        "-c",
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + code,
        timeout=timeout,
    )


@pytest.mark.parametrize("exit_code", [0, 4])
def test_collection_exit_preserved_and_worker_joined(
    tmp_path: Path, pytester: pytest.Pytester, exit_code: int
) -> None:
    result = run_isolated(
        pytester,
        "import faulthandler, threading\n"
        "from scripts.ci.collection_tracebacks import periodic_tracebacks\n"
        f"with open({str(tmp_path / 'stacks.log')!r}, 'w') as stacks:\n"
        "    with periodic_tracebacks(stacks, dump_traceback=faulthandler.dump_traceback):\n"
        f"        raise SystemExit({exit_code})\n",
    )
    assert result.ret == exit_code, str(result.stderr)


# meiva:note
# Three facts made the old shape red on a loaded runner (2026-10-29): a dump is taken on the dumper's
# own schedule, so a loop bounded by wall clock only proves the machine scheduled the dumper inside
# that window; faulthandler prints at most 100 frames per thread, so `recurse(123)` pushed the
# thread's outermost `churn` frame past the cap (measured: invisible at 123, visible at 60 and 20);
# and the dumper's first dump can precede the worker thread's entry, which leaves a file with no
# churn frame at all. So the test takes its evidence in two gated steps instead of one sample: the
# thread parks in its innermost frame and a dump is awaited there (the deep chain must be complete),
# then the frames resume and a second dump is awaited (a moving stack must still be safe to read).
# Both waits are facts the test observes, not windows it hopes the scheduler honors. The resumed
# phase asserts the entry frame only — `churn` is on the thread's stack for as long as its loop runs,
# while a frame inside the recursion needs sampling luck.
# meiva:link scripts/ci/collection_tracebacks.py
def test_frame_churn_finishes_with_complete_tracebacks(
    tmp_path: Path, pytester: pytest.Pytester
) -> None:
    output = tmp_path / "stacks.log"
    result = run_isolated(
        pytester,
        "import faulthandler, threading\n"
        "from scripts.ci.collection_tracebacks import periodic_tracebacks\n"
        "entered = threading.Event()\n"
        "released = threading.Event()\n"
        "stop = threading.Event()\n"
        "parked_dump = threading.Event()\n"
        "moving_dump = threading.Event()\n"
        "def recurse(depth):\n"
        "    a, b, c = 1, 2, 3\n"
        "    if depth:\n"
        "        return recurse(depth - 1)\n"
        "    if not released.is_set():\n"
        "        entered.set()\n"
        "        assert released.wait(10), 'the parked frame was never released'\n"
        "    return a + b + c\n"
        "def churn():\n"
        "    assert recurse(20) == 6\n"
        "    while not stop.is_set():\n"
        "        assert recurse(20) == 6\n"
        "def dump(*, file, all_threads):\n"
        "    if released.is_set():\n"
        "        file.write('churn phase\\n')\n"
        "        file.flush()\n"
        "        faulthandler.dump_traceback(file=file, all_threads=all_threads)\n"
        "        moving_dump.set()\n"
        "        return\n"
        "    faulthandler.dump_traceback(file=file, all_threads=all_threads)\n"
        "    parked_dump.set()\n"
        "worker = threading.Thread(target=churn, name='churning-worker')\n"
        "worker.start()\n"
        f"with open({str(output)!r}, 'w') as stacks:\n"
        "    try:\n"
        "        assert entered.wait(10), 'the churn thread never reached its innermost frame'\n"
        "        with periodic_tracebacks(stacks, interval=0.001, dump_traceback=dump):\n"
        "            assert parked_dump.wait(10), 'no dump was taken while the frame was parked'\n"
        "            released.set()\n"
        "            assert moving_dump.wait(10), 'no dump was taken after the frames resumed'\n"
        "    finally:\n"
        "        stop.set()\n"
        "        released.set()\n"
        "        worker.join()\n"
        "assert not any(t.name == 'collection-tracebacks' for t in threading.enumerate())\n",
        timeout=20,
    )
    assert result.ret == 0, str(result.stderr)
    trace = output.read_text()
    assert "Periodic traceback" in trace
    assert 'File "' in trace and ", line " in trace
    phases = trace.split("churn phase", 1)
    assert len(phases) == 2, "no dump was taken after the frames resumed"
    dumps = phases[0].split("Periodic traceback")
    assert len(dumps) > 1, "no dump was taken while the frame was parked"
    assert "in recurse" in dumps[1] and "in churn" in dumps[1], (
        "the first dump did not show the parked recursion"
    )
    assert "in churn" in phases[1], "the dump after the frames resumed lost the churn entry frame"


@pytest.mark.parametrize(
    ("workflow", "counts"),
    [
        ("ci.yml", ["8", "${{ needs.classify.outputs.shards }}"]),
        ("file-shard-proof.yml", ["16"]),
    ],
)
def test_workflow_runs_the_planner_module(workflow: str, counts: list[str]) -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    scripts = [
        step["run"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if "plan_file_shards" in step.get("run", "")
    ]
    assert len(scripts) == len(counts)
    for script, count in zip(scripts, counts, strict=True):
        assert "-m scripts.ci.pull_requests.plan_file_shards" in script
        assert f"--count {count} " in script


@pytest.mark.parametrize("exit_code", [0, 4])
def test_planner_preserves_verdict(pytester: pytest.Pytester, exit_code: int) -> None:
    (pytester.path / "tmp").mkdir()
    result = run_isolated(
        pytester,
        "import types\n"
        "pytest = types.ModuleType('pytest')\n"
        "def main(args):\n"
        "    print('planner called', args)\n"
        f"    return {exit_code}\n"
        "pytest.main = main\n"
        "sys.modules['pytest'] = pytest\n"
        "from scripts.ci.pull_requests.plan_file_shards import main\n"
        "raise SystemExit(main(['--count', '8', '--plan', 'tmp/plan.json',"
        " '--stacks', 'tmp/plan-stacks.log']))\n",
    )
    assert result.ret == exit_code, str(result.stderr)
    called = str(result.stdout)
    for argument in (
        "--collect-only",
        "--omit-static-tests",
        "--file-shard-count=8",
        "--file-shard-plan=tmp/plan.json",
    ):
        assert argument in called
    assert len(list((pytester.path / "tmp").rglob("*-stacks.log"))) == 1
