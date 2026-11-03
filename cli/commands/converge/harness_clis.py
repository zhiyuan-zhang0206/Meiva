# meiva:note
# Host converge step for local external harness CLIs: ensures installed CLIs match
# this checkout's pinned catalog versions.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

from base.host.env.harness.catalog import HarnessSpec
from base.host.env.harness.convergence import (
    CommandResult,
    converge_local_harness_clis,
)
from cli.commands.converge.spec import ConvergeCtx


def ensure_external_harness_clis(
    ctx: ConvergeCtx, *, harnesses: Sequence[HarnessSpec] | None = None
) -> None:
    def run_shell(command: str, *, timeout: float, env: Mapping[str, str]) -> CommandResult:
        try:
            res = ctx.host.run(
                ["bash", "-c", command],
                timeout=timeout,
                capture_output=True,
                text=True,
                env=dict(env),
            )
            return CommandResult(
                exit_code=res.returncode,
                stdout=res.stdout or "",
                stderr=res.stderr or "",
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                exit_code=124,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=f"command timed out after {timeout}s: {(exc.stderr or '') if isinstance(exc.stderr, str) else ''}",
            )

    report = converge_local_harness_clis(
        user_home=ctx.session.user_home,
        meiva_home=ctx.meiva_home,
        now=ctx.clock.now(),
        source="converge",
        base_env=ctx.config.process_environment(),
        run_shell=run_shell,
        harnesses=harnesses,
    )

    for item in report.updated:
        print(f"    {item.cli}: {item.previous_version or 'unknown'} -> {item.installed_version}")

    if not report.ok:
        raise RuntimeError(
            "external harness CLI convergence failed: "
            + "; ".join(f"{f.cli} ({f.detail})" for f in report.failed)
        )
