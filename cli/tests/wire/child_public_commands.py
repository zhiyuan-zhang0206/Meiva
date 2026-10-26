# meiva:link cli/docs/operator-surfaces/public-client-wire.meiva.okf.md
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from base.cluster.machine import GatewayAccess
from cli.database.runtime import CommandRuntime


def _runtime() -> CommandRuntime:
    return CommandRuntime.create(Path(os.environ["MEIVA_HOME"]))


def _access() -> GatewayAccess:
    from base.cluster.machine.boot import gateway_access_from_boot

    return gateway_access_from_boot(_runtime().config)


def direct(mode: str) -> int:
    from cli.commands.agents import control, parsers
    from cli.commands.management import schedules

    if mode == "schedules-xor-none":
        return schedules.cmd_schedules_create(
            _access(), "n", None, None, None, None, idempotency_key="key"
        )
    if mode == "schedules-xor-both":
        return schedules.cmd_schedules_create(
            _access(), "n", "print(1)", "s.py", None, None, idempotency_key="key"
        )
    if mode == "schedules-update-empty":
        return schedules.cmd_schedules_update(
            _access(), "7", None, None, None, None, None, enabled=None, idempotency_key="key"
        )
    if mode == "restart-invalid-config":
        return control.cmd_agents_restart(_access(), 9, '["not", "an", "object"]')
    if mode == "send-missing-source":
        return control.cmd_agents_send(_access(), 42, "hello", None, idempotency_key="key")
    if mode in {"send-handler-missing", "send-handler-invalid"}:
        return parsers.h_agents_send(
            argparse.Namespace(
                agent_id=42,
                content="hello",
                source=None if mode.endswith("missing") else "bogus",
                tail_file=None,
                completion=False,
                runtime=_runtime(),
            )
        )
    if mode == "kill-handler-invalid":
        return parsers.h_agents_kill(
            argparse.Namespace(
                agent_id=42,
                source="bogus",
                kill_all_shell_sessions=False,
                runtime=_runtime(),
            )
        )
    raise AssertionError(f"unknown direct contract: {mode}")


def invoke(mode: str, argv: list[str]) -> int:
    if mode != "parser":
        assert not argv
        return direct(mode)
    if not argv or argv[0] not in {"agents", "schedules"}:
        raise AssertionError("the wire child accepts only agent and schedule commands")
    from cli.parsers import build_parser

    args = build_parser().parse_args(argv)
    args.runtime = _runtime()
    return args.func(args)


def main() -> None:
    from base.config import settings
    from cli.commands.agents.control import ProvenanceError

    assert settings.profile == "gateway"
    assert not settings.has_domain("agent")
    receipt, mode, expected, status, path, match, *argv = sys.argv[1:]
    if expected == "http":
        with pytest.raises(httpx.HTTPStatusError) as raised:
            invoke(mode, argv)
        assert raised.value.response.status_code == int(status)
        assert raised.value.request.url.path == path
        result = 0
    elif expected in {"value", "provenance"}:
        error = ProvenanceError if expected == "provenance" else ValueError
        with pytest.raises(error, match=match or None):
            invoke(mode, argv)
        result = 0
    else:
        assert expected == ""
        result = invoke(mode, argv)
    Path(receipt).write_text(
        json.dumps(
            {
                "result": result,
                "expected_error": expected,
                "caller_profile": os.environ.get("MEIVA_CALLER_IDENTITY"),
            }
        )
    )
    raise SystemExit(result)


if __name__ == "__main__":
    main()
