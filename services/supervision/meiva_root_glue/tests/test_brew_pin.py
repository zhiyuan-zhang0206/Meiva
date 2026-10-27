import subprocess
from pathlib import Path

import pytest

from base.daemon.health import ProbeVerdict
from services.supervision.meiva_root_glue.diagnostic_probes import brew_pins

# meiva:note
# The process environment the root hands the check; the brew stand-ins need a shell and sleep, and
# nothing of this machine's own PATH (2026-10-27).
_ENV = {"PATH": "/bin:/usr/bin"}


def test_missing_approved_pin_is_reported(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text(
        "#!/bin/sh\nif [ \"$2\" = \"--pinned\" ]; then\n  printf 'postgresql@17\\n'\nelse\n  printf 'postgresql@17\\nredis\\n'\nfi\n"
    )
    executable.chmod(0o700)
    assert brew_pins(environment=_ENV, brew_binary=str(executable)).verdict.value == "down"


def test_command_failure_is_not_an_empty_pin_set(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    with pytest.raises(subprocess.CalledProcessError):
        brew_pins(environment=_ENV, brew_binary=str(executable))


def test_pin_observation_uses_real_bounded_process_contract(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text("#!/bin/sh\nprintf 'postgresql@17\\n'\n")
    executable.chmod(0o700)
    assert brew_pins(environment=_ENV, brew_binary=str(executable)).alive


def test_nonzero_brew_query_is_not_readiness(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text("#!/bin/sh\nexit 7\n")
    executable.chmod(0o700)
    with pytest.raises(subprocess.CalledProcessError):
        brew_pins(environment=_ENV, brew_binary=str(executable))


# meiva:note
# The false alarm this check produced (2026-10-27, #5656): a brew query slower than the retired
# five-second literal was reported as a broken pin set while the environment was fine. A leg that
# is slow but inside the check's own deadline is a healthy reading.
def test_a_leg_slower_than_the_retired_five_second_bound_is_not_a_failure(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text(
        '#!/bin/sh\nif [ "$2" = "--pinned" ]; then\n  sleep 6\nfi\nprintf \'postgresql@17\\n\'\n'
    )
    executable.chmod(0o700)
    assert brew_pins(environment=_ENV, brew_binary=str(executable)).alive


# meiva:note
# Nothing was read, so nothing may be concluded: a leg that outlives the check's deadline is
# inconclusive, and the detail names the leg and the deadline that produced it.
def test_a_leg_that_outlives_the_deadline_is_inconclusive_not_down(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text("#!/bin/sh\nsleep 30\n")
    executable.chmod(0o700)

    probe = brew_pins(environment=_ENV, brew_binary=str(executable), deadline_s=2.0)

    assert probe.verdict is ProbeVerdict.INCONCLUSIVE
    assert "brew list --pinned did not finish within its share of the 2s observation deadline" in (
        probe.detail
    )
    assert "brew list --formula did not finish within its share of the 2s observation deadline" in (
        probe.detail
    )


# meiva:note
# One hung query cannot starve the other: the second query is attempted after the first expires and
# its answer is used. The verdict still has no conclusion — half a pin set is not a reading — but
# the expired query is the only one reported as missing, never the one that answered (2026-10-27).
def test_the_second_query_still_answers_after_the_first_expires(tmp_path: Path) -> None:
    marker = tmp_path / "formula-query-ran"
    executable = tmp_path / "brew"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = "--pinned" ]; then\n  sleep 30\nfi\n'
        f"touch {marker}\n"
        "printf 'postgresql@17\\n'\n"
    )
    executable.chmod(0o700)

    probe = brew_pins(environment=_ENV, brew_binary=str(executable), deadline_s=2.0)

    assert marker.exists(), "the second query must be attempted after the first one expires"
    assert probe.verdict is ProbeVerdict.INCONCLUSIVE
    assert "brew list --pinned did not finish" in probe.detail
    assert "--formula" not in probe.detail


# meiva:note
# The same rule on the other side: when the query that still has time fails, its failure is
# reported rather than swallowed by the one that expired.
def test_the_second_query_reports_its_own_failure_after_the_first_expires(tmp_path: Path) -> None:
    executable = tmp_path / "brew"
    executable.write_text('#!/bin/sh\nif [ "$2" = "--pinned" ]; then\n  sleep 30\nfi\nexit 7\n')
    executable.chmod(0o700)

    with pytest.raises(subprocess.CalledProcessError):
        brew_pins(environment=_ENV, brew_binary=str(executable), deadline_s=2.0)
