from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import meiva
from agent.tests.impersonation.external.runtime import Takeover


def test_expired_lease_cannot_dispatch_through_meiva_mcps(
    attached_mcp_runtime: tuple[Takeover, Any], unit_home: Path
) -> None:
    takeover, attachment = attached_mcp_runtime
    (unit_home / "mcp.json").write_text(
        json.dumps({"mcpServers": {"example": {"command": "true"}}}), encoding="utf-8"
    )
    takeover.expire()
    try:
        with pytest.raises(RuntimeError, match="not active"):
            meiva.mcps.example.raw("side_effect")
    finally:
        with pytest.raises(RuntimeError, match="not active"):
            attachment.close()
