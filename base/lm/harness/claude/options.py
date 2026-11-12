from __future__ import annotations

from collections.abc import Collection, Mapping
from uuid import NAMESPACE_URL, uuid5

from base.lm.harness.claude.auth import build_env
from base.lm.harness.claude.config import BridgeConfig


# meiva:note
# The session id a fork's clone gets, fixed by the fork's own agent id. The CLI accepts a
# caller-chosen id together with `fork_session`, and refuses to start under one that already
# exists, so a start that crashed after the CLI wrote the clone is told apart from one that
# never cloned by asking whether this id has a transcript: present resumes it, absent forks.
# That makes the clone idempotent without a second record.
# meiva:link base/lm/harness/docs/fork.md
def fork_session_id(agent_id: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"https://meiva.local/claude-code-fork-session/{agent_id}"))


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
    fork_to: str | None = None,
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
        forward_subagent_text=True,
        env=build_env(environ, config.auth, host=environ),
        resume=resume,
    )
    if config.session_store is not None:
        # meiva:note
        # The transcript mirror, flushed eagerly, only wakes the timeline writer: the store ignores the rows it is
        # handed and `load` answers nothing, so a resume still reads the CLI's own file.
        # meiva:link base/lm/harness/docs/transcript-projection.md
        if "session_store" not in option_fields or "session_store_flush" not in option_fields:
            raise ValueError("the installed Claude SDK has no session store option")
        values.update(session_store=config.session_store, session_store_flush="eager")
    if fork_to is not None:
        # meiva:note
        # The clone is made by the same options every session of this agent starts with: only
        # the two fork keys are added, so the model, system prompt, tools and environment that
        # reach the prefix are the source's own and the clone's first request reuses its cache.
        values.update(fork_session=True, session_id=fork_to)
    return values
