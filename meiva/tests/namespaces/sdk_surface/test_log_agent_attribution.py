"""Ordinary logs use explicit caller or process attribution, never a turn fallback."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import meiva
from base.agents.context.identity import AgentIdentity
from base.host.proc import run_bounded
from base.telemetry.process_logging import message_to_params
from meiva.sdk_surface import process_context

_GATEWAY_BOOT_CHILD = Path(__file__).with_name("child_gateway_boot.py")


class _FakeMessage:
    """The shape `message_to_params` reads off a loguru message."""

    def __init__(self, extra: dict[str, object]) -> None:
        from datetime import UTC, datetime

        self.record = {
            "time": datetime.now(UTC),
            "extra": extra,
            "message": "hello",
            "exception": None,
            "level": type("L", (), {"name": "INFO"})(),
        }


def _agent_id_of(extra: dict[str, object]) -> int | None:
    _ts, agent_id, _level, _event, _payload, _source = message_to_params(_FakeMessage(extra))  # pyright: ignore[reportArgumentType]
    return agent_id


def test_unannotated_log_has_no_agent() -> None:
    process_context.bind_context(replace(meiva.context, identity=AgentIdentity(42, True)))
    assert _agent_id_of({"agent_id": "-", "event": "log"}) is None


def test_explicit_agent_id_is_preserved() -> None:
    process_context.bind_context(replace(meiva.context, identity=AgentIdentity(42, True)))
    assert _agent_id_of({"agent_id": "99", "event": "log"}) == 99


def test_missing_required_log_field_fails() -> None:
    with pytest.raises(KeyError, match="agent_id"):
        _agent_id_of({"event": "log"})


def test_transport_source_preserves_usage_payload_source() -> None:
    _ts, _agent_id, _level, _event, payload, source = message_to_params(
        _FakeMessage(  # pyright: ignore[reportArgumentType]
            {
                "agent_id": "-",
                "event": "llm_usage",
                "source": "web.fetch",
                "transport_source": "system",
            }
        )
    )
    assert source == "system"
    assert payload["source"] == "web.fetch"


def test_gateway_boot_retains_process_generation_evidence(unit_home: Path) -> None:
    result = run_bounded(
        [sys.executable, str(_GATEWAY_BOOT_CHILD), "loaded-commit"],
        env={**os.environ, "MEIVA_HOME": str(unit_home), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["agent_id"] is None
    assert payload["process"] == "agent_host"
    assert payload["machine"] == "machine-a"
    fields = payload["attributes"]
    assert fields["name"] == "agent_host"
    assert fields["sha"] == "loaded-generation"
    assert fields["host"] == "machine-a"
    assert fields["pid"] > 0


def test_owned_gateway_boot_uses_loaded_image_and_original_producer(unit_home: Path) -> None:
    result = run_bounded(
        [sys.executable, str(_GATEWAY_BOOT_CHILD), "owned-image"],
        env={**os.environ, "MEIVA_HOME": str(unit_home), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["process"] == "agent_host"
    assert payload["machine"] == "owned-machine"
    assert payload["producer_calls"] >= 1
    fields = payload["attributes"]
    assert fields["sha"] == "already-loaded"
    assert fields["host"] == "owned-machine"
