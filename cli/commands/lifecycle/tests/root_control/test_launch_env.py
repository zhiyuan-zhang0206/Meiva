# meiva:note
# The environment the launcher hands the root, and the launch digest bound to it.
#
# The root's environment is built from the launcher's own, so each case runs the builder in a
# child process that has exactly the environment under test (`_launch_env_child.py`).

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.host.env.registry import FIELD_ALIASES, HOST_PASSTHROUGH_KEYS, NETWORK_PROXY_KEYS
from base.host.tests.short_tmp import short_tmp as short_tmp
from cli.commands.lifecycle.tests.root_control.support import git_repo

REPO_ROOT = Path(__file__).resolve().parents[5]
CHILD = Path(__file__).with_name("_launch_env_child.py")

# meiva:note
# What each service manager adds to its job's environment on its own. systemd with `User=` sets
# USER/LOGNAME/SHELL, plus its invocation bookkeeping; a launchd job also gets its per-user
# TMPDIR, the agent socket and XPC names.
SERVICE_MANAGER_INJECTIONS = {
    "systemd User=": {
        "USER": "meiva-owner",
        "LOGNAME": "meiva-owner",
        "SHELL": "/bin/sh",
        "INVOCATION_ID": "0" * 32,
        "JOURNAL_STREAM": "8:12345",
        "SYSTEMD_EXEC_PID": "4242",
    },
    "launchd": {
        "USER": "meiva-owner",
        "LOGNAME": "meiva-owner",
        "SHELL": "/bin/zsh",
        "TMPDIR": "/private/var/folders/xy/T/",
        "SSH_AUTH_SOCK": "/private/tmp/com.apple.launchd.x/Listeners",
        "XPC_SERVICE_NAME": "0",
        "XPC_FLAGS": "0x0",
        "OSLogRateLimit": "64",
    },
}


# meiva:note
# The child's one line of output, run with `env` and nothing else of this process's.
def run_child(home: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(CHILD), *args],
        cwd=REPO_ROOT,
        env={**env, "MEIVA_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_root_launch_path_uses_home_declaration_across_callers(
    short_tmp: Path, tmp_path: Path
) -> None:
    declared = str(tmp_path / "tools")
    interactive = run_child(short_tmp, {"PATH": str(tmp_path / "interactive")}, "env", declared)
    systemd = run_child(short_tmp, {"PATH": str(tmp_path / "systemd")}, "env", declared)
    redeclared = run_child(
        short_tmp, {"PATH": str(tmp_path / "interactive")}, "env", str(tmp_path / "changed")
    )

    assert systemd == interactive
    assert redeclared != interactive


# meiva:note
# The started root and the stage observer derive one launch digest.
#
# The start action runs inside the boot unit, whose manager injects variables of its own; the
# observer runs with the fixed stage environment only. Both sides must still name the same
# immutable generation.
@pytest.mark.parametrize("manager", sorted(SERVICE_MANAGER_INJECTIONS))
def test_root_launch_digest_ignores_service_manager_injections(
    short_tmp: Path, tmp_path: Path, manager: str
) -> None:
    repo = git_repo(tmp_path / "repo")
    declared = str(tmp_path / "tools")
    stage = {"PATH": os.environ["PATH"]}

    observed = run_child(short_tmp, stage, "digest", declared, str(repo), str(short_tmp))
    started = run_child(
        short_tmp,
        {**stage, **SERVICE_MANAGER_INJECTIONS[manager]},
        "digest",
        declared,
        str(repo),
        str(short_tmp),
    )
    changed = run_child(
        short_tmp, stage, "digest", str(tmp_path / "changed"), str(repo), str(short_tmp)
    )

    assert started == observed
    assert changed != observed


# meiva:note
# Every key root hands its units is a declared launch input or an ambient host fact.
#
# `launch_input_keys` is a positive list, so a non-ambient key a delivery adds would silently
# stay out of the launch digest; this names it instead. Every key the registry can forward
# (each passthrough row, every settings alias) is set, including keys forwarded only when
# present, so what this catches does not depend on this host's environment.
def test_root_env_beyond_the_launch_inputs_is_only_ambient(short_tmp: Path, tmp_path: Path) -> None:
    paths = {"HOME", "PATH", "TMPDIR", "TEMP", "TMP"}
    declared = (
        HOST_PASSTHROUGH_KEYS
        | NETWORK_PROXY_KEYS
        | set(FIELD_ALIASES.values())
        | paths
        | {"PYTHONUTF8"}
    )
    values = {
        "PYTHONUTF8": "1",
        **dict.fromkeys(paths, str(tmp_path)),
        # meiva:note
        # the interpreter parses these
    }
    env = {key: values.get(key, "ambient") for key in declared}

    undeclared = json.loads(run_child(short_tmp, env, "undeclared", str(tmp_path / "tools")))

    assert not undeclared, f"declare these in launch_input_keys or as ambient: {undeclared}"
