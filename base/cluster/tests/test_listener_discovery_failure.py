from __future__ import annotations

import pytest

from base.cluster import listener_plan, port_preflight


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (2, "", "timeout"),
        (1, "p1109\n", ""),
        (1, "", "permission denied"),
    ],
)
def test_failed_listener_discovery_is_unavailable(
    returncode: int, stdout: str, stderr: str
) -> None:
    with pytest.raises(port_preflight.ListenerDiscoveryError):
        listener_plan.parse_lsof_pids(4319, returncode, stdout, stderr)


@pytest.mark.parametrize(
    ("stdout", "code", "expected"), [("p1109\np1109\n", 0, [1109]), ("", 1, [])]
)
def test_successful_lsof_distinguishes_listeners_from_absence(
    stdout: str, code: int, expected: list[int]
) -> None:
    pids = listener_plan.parse_lsof_pids(4319, code, stdout, "")
    assert listener_plan.reconcile_strict_listeners(4319, None, pids) == expected


def test_probe_resolves_lsof_through_candidates_in_restricted_context() -> None:
    argv = listener_plan.resolve_lsof_argv(
        "-nP",
        "-Fp",
        "-sTCP:LISTEN",
        "-iTCP:4319",
        path_binary=None,
        executable_candidates={"/usr/sbin/lsof"},
    )
    assert argv == ["/usr/sbin/lsof", "-nP", "-Fp", "-sTCP:LISTEN", "-iTCP:4319"]
