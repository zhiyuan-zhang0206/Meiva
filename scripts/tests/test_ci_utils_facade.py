"""External status imports and executable CLI path remain stable."""

import subprocess
import sys

from scripts.ci import cli as ci_utils
from scripts.ci.pull_requests import commands, status


def test_public_ci_symbols_keep_their_single_owner() -> None:
    assert ci_utils.CIStatus is status.CIStatus
    assert ci_utils.CIResult is status.CIResult
    assert ci_utils.check_ci is status.check_ci
    assert ci_utils.main is commands.main


def test_executable_cli_help_no_longer_offers_the_retired_queue() -> None:
    # meiva:note
    # The Trunk merge queue was retired on 2026-11-01 (PR #8081) and its submission
    # flags were deleted (PR #8153). The help output is the executable contract: a
    # queue flag that still appeared here would promise a merge path that cannot run,
    # and the caller would only learn otherwise after queuing a pull request whose
    # every job skips. The subprocess below runs this repository's own CLI for --help.
    result = subprocess.run(  # noqa: S603
        [sys.executable, ci_utils.__file__, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--wait" in result.stdout
    assert "--force" not in result.stdout
    for retired in ("--merge", "--queue", "--queue-status", "--evict", "--priority"):
        assert retired not in result.stdout, retired
