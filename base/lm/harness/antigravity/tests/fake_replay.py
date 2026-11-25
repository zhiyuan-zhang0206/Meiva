from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from base.lm.harness.antigravity.tests.fake_wire import SID, frame, record

STREAMING = Path(__file__).parent / "fixtures" / "streaming"


def _lines(scenario: str, name: str) -> list[dict[str, Any]]:
    text = (STREAMING / scenario / name).read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def replay_prompt(transcript: Path, request: int, scenario: str) -> None:
    for line in _lines(scenario, "record.jsonl"):
        if line["row"]["type"] != "USER_INPUT":
            record(transcript, line["row"])
    for line in _lines(scenario, "stream.jsonl"):
        if line.get("method") == "session/update":
            frame(
                {
                    "method": "session/update",
                    "params": json.loads(json.dumps(line["params"]).replace("$session", SID)),
                }
            )
        elif "result" in line:
            frame({"id": request, "result": line["result"]})
