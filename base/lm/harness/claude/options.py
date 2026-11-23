from __future__ import annotations

from collections.abc import Collection, Mapping
from uuid import NAMESPACE_URL, uuid5

from base.lm.harness.claude.auth import build_env
from base.lm.harness.claude.config import BridgeConfig


def telemetry_env(endpoint: str, agent_id: int, *, trace_enabled: bool) -> dict[str, str]:
    endpoint = endpoint.rstrip("/")
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1" if trace_enabled else "0",
        "ENABLE_BETA_TRACING_DETAILED": "0",
        "BETA_TRACING_ENDPOINT": "",
        "OTEL_SERVICE_NAME": "claude-code",
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"meiva.agent_id={agent_id},harness=claude_code,service.instance.id=meiva-agent-{agent_id}"
        ),
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": f"{endpoint}/v1/logs",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{endpoint}/v1/traces",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": f"{endpoint}/v1/metrics",
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp" if trace_enabled else "none",
        "OTEL_LOG_USER_PROMPTS": "0",
        "OTEL_LOG_ASSISTANT_RESPONSES": "0",
        "OTEL_LOG_TOOL_DETAILS": "0",
        "OTEL_LOG_TOOL_CONTENT": "0",
        "OTEL_LOG_MANAGED_SETTINGS": "0",
        "OTEL_LOG_RAW_API_BODIES": "0",
        "OTEL_METRICS_INCLUDE_SESSION_ID": "false",
        "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
        "OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES": "false",
        "OTEL_METRICS_INCLUDE_REPOSITORY": "false",
    }


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
    env = build_env(environ, config.auth, host=environ)
    env.update(TRACEPARENT="", TRACESTATE="", CLAUDE_CODE_ENABLE_TELEMETRY="0")
    if config.otlp_endpoint is not None and config.agent_id is not None:
        env.update(
            telemetry_env(config.otlp_endpoint, config.agent_id, trace_enabled=config.trace_enabled)
        )
    values.update(
        system_prompt={"type": "preset", "preset": "claude_code"},
        model=config.model,
        cwd=str(config.cwd),
        tools=None if config.tools is None else list(config.tools),
        permission_mode=config.permission_mode,
        include_partial_messages=True,
        max_buffer_size=config.max_buffer_size,
        forward_subagent_text=True,
        env=env,
        resume=resume,
    )
    if fork_to is not None:
        # meiva:note
        # The clone is made by the same options every session of this agent starts with: only
        # the two fork keys are added, so the model, system prompt, tools and environment that
        # reach the prefix are the source's own and the clone's first request reuses its cache.
        values.update(fork_session=True, session_id=fork_to)
    return values
