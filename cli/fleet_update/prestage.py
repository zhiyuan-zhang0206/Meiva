# meiva:note
# Warm NEW's dependency caches and external harness CLIs while the cluster is still serving, so
# neither `switch` nor `meiva start` spends the outage downloading wheels, npm tarballs or agent
# harness binaries. Nothing inside `$S` or `$S/.venv` is mutated here.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import shlex

from .plan.host_commands import HOME

_PRESTAGE_PATHS = (
    "pyproject.toml uv.lock .python-version base cli ui/web/package.json ui/web/pnpm-lock.yaml"
)
_PRESTAGE_HARNESS_SCRIPT = """import os, subprocess
from datetime import UTC, datetime
from pathlib import Path
from base.host.env.dotenv_file import read_env_aliases
from base.host.env.harness.convergence import CommandResult, converge_local_harness_clis

home = Path(os.environ["MEIVA_HOME"])
raw = read_env_aliases(home / ".env")
if raw.get("MEIVA_MACHINE_SERVE_AGENT_RUNNER", "").strip().lower() in ("1", "true", "yes", "on"):
    def _run(cmd: str, *, timeout: float, env: dict[str, str]) -> CommandResult:
        try:
            r = subprocess.run(["bash", "-c", cmd], timeout=timeout, capture_output=True, text=True, env=dict(env))
            return CommandResult(exit_code=r.returncode, stdout=r.stdout or "", stderr=r.stderr or "")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else ""
            err = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(exit_code=124, stdout=out, stderr=f"command timed out after {timeout}s: {err}")
    rep = converge_local_harness_clis(
        user_home=Path.home(),
        meiva_home=home,
        now=datetime.now(UTC),
        source="converge",
        base_env={**os.environ, **raw},
        run_shell=_run,
    )
    for item in rep.updated:
        print(f"prestage {item.cli}: {item.previous_version or 'unknown'} -> {item.installed_version}")
    if not rep.ok:
        raise SystemExit(
            "prestage external harness CLI convergence failed: "
            + "; ".join(f"{f.cli} ({f.detail})" for f in rep.failed)
        )
"""


def prestage_command(new: str) -> str:
    quoted = shlex.quote(new)
    return (
        f"{HOME}\n"
        'tmp=$(mktemp -d "${TMPDIR:-/tmp}/meiva-prestage.XXXXXX")\n'
        "trap 'rm -rf \"$tmp\"' EXIT\n"
        f'git -C "$S" archive {quoted} -- {_PRESTAGE_PATHS} | tar -x -C "$tmp"\n'
        f'if ! git -C "$S" diff --quiet HEAD {quoted} -- pyproject.toml uv.lock .python-version; then\n'
        '  (cd "$tmp" && UV_PROJECT_ENVIRONMENT="$tmp/.venv" env -u VIRTUAL_ENV '
        "uv sync -q --frozen --no-install-project --no-build-package meiva)\n"
        "fi\n"
        'if [ -d "$S/ui/web/node_modules" ] && [ -f "$tmp/ui/web/pnpm-lock.yaml" ] '
        f'&& ! git -C "$S" diff --quiet HEAD {quoted} -- ui/web/pnpm-lock.yaml '
        "&& command -v pnpm >/dev/null 2>&1; then\n"
        '  (cd "$tmp/ui/web" && CI=true pnpm fetch --frozen-lockfile >/dev/null)\n'
        "fi\n"
        'if [ -f "$tmp/base/host/env/harness/convergence.py" ]; then\n'
        '  py="$S/.venv/bin/python"\n'
        '  [ -x "$tmp/.venv/bin/python" ] && py="$tmp/.venv/bin/python"\n'
        f'  PYTHONPATH="$tmp" MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip "$py" -c {shlex.quote(_PRESTAGE_HARNESS_SCRIPT)}\n'
        "fi"
    )
