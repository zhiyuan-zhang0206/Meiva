from __future__ import annotations

from collections.abc import Collection, Mapping

from base.lm.harness.claude.auth import build_env
from base.lm.harness.claude.config import BridgeConfig


# meiva:note
# The keyword values one session hands the SDK's option class, and the two that ride only when
# they are real. `cli_path` rides only when the CLI was resolved: a machine without the extra
# has no path to pin, and an injected SDK is a test double that must not be asked to run the
# binary. `thinking` rides only when the option class has the field at all — Python's own
# Claude Code SDK takes it, while a test double that models the older option set would reject
# it. Everything else is the boundary this adapter promises the CLI: the model's full id, the
# workspace as the CLI's cwd, the capability list, no approval layer, no MCP servers of ours,
# partial messages on, and the allowlisted child environment.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
def option_values(
    config: BridgeConfig,
    *,
    environ: Mapping[str, str],
    cli_path: str | None,
    option_fields: Collection[str],
    resume: str | None,
) -> dict[str, object]:
    values: dict[str, object] = {}
    if cli_path is not None:
        values["cli_path"] = cli_path
    if "thinking" in option_fields:
        values["thinking"] = {"type": "adaptive", "display": "summarized"}
    if config.reasoning_effort is not None:
        if "effort" not in option_fields:
            raise ValueError("the installed Claude SDK does not support the selected effort")
        values["effort"] = config.reasoning_effort
    values.update(
        model=config.model,
        cwd=str(config.cwd),
        tools=None if config.tools is None else list(config.tools),
        permission_mode=config.permission_mode,
        max_turns=config.max_turns,
        include_partial_messages=True,
        env=build_env(environ, config.auth, host=environ),
        resume=resume,
    )
    return values
