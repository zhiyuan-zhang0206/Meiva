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
# partial messages on, `replay-user-messages` so every input is echoed with its uuid (the
# acknowledgement the adapter rests on; an SDK that cannot pass it is refused), and the allowlisted
# child environment.
#
# `system_prompt` is the CLI's own `claude_code` preset, named explicitly. Left unset, the SDK
# sends `--system-prompt ""` and the model gets a ~485-character stub instead of the harness's
# default prompt; the preset without `append` sends no flag, so the CLI's prompt stands untouched.
# An SDK that cannot take the option fails the session rather than quietly running on the stub.
# meiva:link base/lm/harness/claude/docs/claude.md
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
    if "extra_args" not in option_fields:
        raise ValueError("the installed Claude SDK cannot pass replay-user-messages to the CLI")
    values["extra_args"] = {"replay-user-messages": None}
    if config.reasoning_effort is not None:
        if "effort" not in option_fields:
            raise ValueError("the installed Claude SDK does not support the selected effort")
        values["effort"] = config.reasoning_effort
    if "system_prompt" not in option_fields:
        raise ValueError("the installed Claude SDK does not support the system prompt preset")
    values.update(
        system_prompt={"type": "preset", "preset": "claude_code"},
        model=config.model,
        cwd=str(config.cwd),
        tools=None if config.tools is None else list(config.tools),
        permission_mode=config.permission_mode,
        include_partial_messages=True,
        env=build_env(environ, config.auth, host=environ),
        resume=resume,
    )
    return values
