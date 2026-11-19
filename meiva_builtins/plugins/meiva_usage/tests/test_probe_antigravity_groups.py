# meiva:note
# What the Antigravity probe says when the quota payload carries a group that no card shows.

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from base.log import logger
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig
from meiva_builtins.plugins.meiva_usage.probes import HttpJsonRequest, probe_antigravity


def test_probe_antigravity_warns_about_a_group_no_card_shows(tmp_path: Path) -> None:
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {"token": {"access_token": "valid-direct-token", "refresh_token": "valid-refresh"}}
        ),
        encoding="utf-8",
    )
    collector = CollectorConfig(
        id="ag-main", label="AG Main", provider="antigravity", machine="test-box"
    )

    def group(name: str, prefix: str) -> dict[str, Any]:
        return {
            "displayName": name,
            "buckets": [{"bucketId": f"{prefix}-5h", "remainingFraction": 0.5}],
        }

    def fake_http_fetch(req: HttpJsonRequest) -> dict[str, Any]:
        return {
            "response": {
                "groups": [
                    group("Gemini Models", "gemini"),
                    group("Gemini Pro Models", "gemini-pro"),
                ]
            }
        }

    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        readings = probe_antigravity(
            collector,
            user_home=tmp_path,
            environ={},
            now=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            platform="darwin",
            http_fetch=fake_http_fetch,
        )
    finally:
        logger.remove(sink)

    assert "ag-main-gemini" in readings
    warned = [line for line in lines if "no card shows" in line]
    assert len(warned) == 1 and "Gemini Pro Models (gemini)" in warned[0], lines
