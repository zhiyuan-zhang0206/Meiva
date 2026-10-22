from __future__ import annotations

from collections.abc import Callable

import pytest

from cli import fleet_update
from cli.tests.fleet_update.fleet_update_cluster import Cluster


# meiva:note
# The verification answers for every host, whatever the others did: one machine's red smoke must
# not delete what the window learned about the rest, so the run still verifies them and still
# reports a line per host.
def test_one_red_smoke_still_verifies_and_reports_every_host(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("smoke", "mac")] = 1
    assert run("up") == fleet_update.PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    assert cluster.smoked == ["gw", "mac", "lin"]
    matrix = [line.partition("matrix: ")[2] for line in out.splitlines() if "matrix: " in line]
    assert [row.split()[0] for row in matrix] == ["gw", "mac", "lin"]
    assert matrix[0].endswith("serving") and matrix[2].endswith("serving")
    assert matrix[1].startswith("mac failed at 'smoke'")
    assert "plugins verify" in out and "packages refresh" in out


def test_a_host_that_never_started_is_reported_skipped_not_missing(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("start", "lin")] = 1
    assert run("up") == fleet_update.PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    matrix = [line.partition("matrix: ")[2] for line in out.splitlines() if "matrix: " in line]
    assert len(matrix) == 3
    assert matrix[0].startswith("gw ") and matrix[1].startswith("mac ")
    assert matrix[2].startswith("lin failed at 'start'")
    assert cluster.smoked == ["gw", "mac"]


def test_a_dry_run_says_the_helper_gate_was_not_evaluated(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = env
    assert run("run", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "helper gate: NOT evaluated" in out
    assert out.count("matrix: ") == 3 and "skipped: dry run" in out
