from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_ACTION = _ROOT / ".github/actions/pnpm-install/action.yml"


def test_all_workflow_frontend_installs_share_the_bounded_action() -> None:
    installs = []
    for path in sorted((_ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", []):
                assert "pnpm install --frozen-lockfile" not in step.get("run", ""), path
                if step.get("uses") == "./.github/actions/pnpm-install":
                    installs.append(step)
    assert len(installs) == 20
    assert all(step["timeout-minutes"] == 5 for step in installs)


@pytest.mark.parametrize(
    ("mode", "expected", "attempts"),
    [
        ("success", 0, 1),
        ("retry", 0, 2),
        ("failure", 42, 2),
        ("hang", 124, 2),
        ("ignore-term", 137, 2),
        ("exit-leak", 0, 1),
    ],
)
def test_install_executes_bounded_attempts(
    tmp_path: Path, mode: str, expected: int, attempts: int
) -> None:
    action = yaml.safe_load(_ACTION.read_text())
    step = action["runs"]["steps"][0]
    assert step["working-directory"] == "ui/web"
    pnpm = tmp_path / "pnpm"
    pnpm.write_text(
        "#!/bin/bash\n"
        '[[ "$*" == "install --frozen-lockfile" ]] || exit 99\n'
        'echo attempt >> "$CALLS"\n'
        'case "$MODE" in\n'
        "  success) exit 0 ;;\n"
        '  retry) [[ "$(wc -l < "$CALLS")" == 2 ]] ;;\n'
        "  failure) exit 42 ;;\n"
        "  hang) echo 'Done in 0.1s using pnpm'; sleep 30 ;;\n"
        "  ignore-term) trap '' TERM; sleep 30 ;;\n"
        "  exit-leak) sleep 30 & echo 'Done but child holds stdout'; exit 0 ;;\n"
        "esac\n"
    )
    pnpm.chmod(0o755)
    timeout = tmp_path / "timeout"
    timeout.write_text(
        "#!/bin/bash\n"
        '[[ "$1 $2 $3" == "--verbose --kill-after=10s 120s" ]] || exit 99\n'
        "shift 3\n"
        'exec /usr/bin/timeout --kill-after=0.1s 0.2s "$@"\n'
    )
    timeout.chmod(0o755)
    calls = tmp_path / "calls"
    # meiva:note
    # Execute the checked-in action, with only test-owned executables on the path.
    # meiva:link .github/actions/pnpm-install/action.yml
    result = subprocess.run(  # noqa: S603
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin", "MODE": mode, "CALLS": str(calls)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert len(calls.read_text().splitlines()) == attempts
    if mode == "hang":
        assert result.stdout.count("Done in 0.1s") == 2
    if expected:
        assert "failed after two bounded attempts" in result.stdout
