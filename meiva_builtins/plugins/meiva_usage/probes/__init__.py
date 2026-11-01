# meiva:note
# Probe dispatchers and implementations for supported LLM providers.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.probes.anthropic_costs import probe_anthropic
from meiva_builtins.plugins.meiva_usage.probes.antigravity import probe_antigravity
from meiva_builtins.plugins.meiva_usage.probes.claude import (
    probe_claude,
    read_macos_keychain_claude_credentials,
)
from meiva_builtins.plugins.meiva_usage.probes.codex import (
    CodexRpcRunner,
    default_codex_rpc_runner,
    find_codex_binary,
    probe_codex,
)
from meiva_builtins.plugins.meiva_usage.probes.deepseek import probe_deepseek
from meiva_builtins.plugins.meiva_usage.probes.http_client import urllib_json_fetcher
from meiva_builtins.plugins.meiva_usage.probes.openai_costs import probe_openai
from meiva_builtins.plugins.meiva_usage.probes.transport import (
    HttpJsonFetcher,
    HttpJsonRequest,
    HttpStatusError,
)
from meiva_builtins.plugins.meiva_usage.types import CardReading, UsageProbeError

__all__ = (
    "CodexRpcRunner",
    "HttpJsonFetcher",
    "HttpJsonRequest",
    "HttpStatusError",
    "default_codex_rpc_runner",
    "find_codex_binary",
    "probe_anthropic",
    "probe_antigravity",
    "probe_claude",
    "probe_codex",
    "probe_collector",
    "probe_deepseek",
    "probe_openai",
    "read_macos_keychain_claude_credentials",
    "urllib_json_fetcher",
)


def probe_collector(
    collector: CollectorConfig,
    *,
    user_home: Path,
    environ: Mapping[str, str],
    now: datetime,
    platform: str,
    http_fetch: HttpJsonFetcher | None = None,
    keychain_reader: Callable[[str], str | None] | None = None,
    codex_rpc_runner: CodexRpcRunner | None = None,
    display_tz: tzinfo = UTC,
    timeout_s: float = 10.0,
) -> dict[str, CardReading]:
    fetcher = http_fetch if http_fetch is not None else urllib_json_fetcher

    if collector.provider == "antigravity":
        return probe_antigravity(
            collector,
            user_home=user_home,
            environ=environ,
            now=now,
            platform=platform,
            http_fetch=fetcher,
            display_tz=display_tz,
        )

    reading: CardReading
    if collector.provider == "claude":
        reading = probe_claude(
            collector,
            user_home=user_home,
            environ=environ,
            now=now,
            platform=platform,
            http_fetch=fetcher,
            keychain_reader=keychain_reader,
            display_tz=display_tz,
        )
    elif collector.provider == "deepseek":
        reading = probe_deepseek(
            collector,
            user_home=user_home,
            environ=environ,
            now=now,
            platform=platform,
            http_fetch=fetcher,
            display_tz=display_tz,
        )
    elif collector.provider == "codex":
        reading = probe_codex(
            collector,
            user_home=user_home,
            environ=environ,
            now=now,
            platform=platform,
            codex_rpc_runner=codex_rpc_runner,
            display_tz=display_tz,
            timeout_s=timeout_s,
        )
    elif collector.provider == "openai":
        reading = probe_openai(
            collector,
            user_home=user_home,
            environ=environ,
            now=now,
            platform=platform,
            http_fetch=fetcher,
            display_tz=display_tz,
        )
    elif collector.provider == "anthropic":
        reading = probe_anthropic(
            collector,
            user_home=user_home,
            environ=environ,
            now=now,
            platform=platform,
            http_fetch=fetcher,
            display_tz=display_tz,
        )
    else:
        raise UsageProbeError(f"Unsupported provider: {collector.provider}")

    return {collector.id: reading}
