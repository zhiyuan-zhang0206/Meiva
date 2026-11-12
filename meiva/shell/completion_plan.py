# meiva:note
# Durable completion intent shared by the shell sender and host recovery.

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, TypeAdapter
from pydantic.dataclasses import dataclass


@dataclass(frozen=True, config=ConfigDict(strict=True, extra="forbid"))
class Completion:
    agent_id: int
    source: str
    content: str
    tail_file: str
    completion: bool
    command_exit: int
    version: Literal[1] = 1

    @property
    def key(self) -> str:
        identity = json.dumps([self.agent_id, self.source, self.tail_file])
        return "completion-" + hashlib.sha256(identity.encode()).hexdigest()

    def argv(self) -> list[str]:
        return [
            "agents",
            "send",
            str(self.agent_id),
            self.content,
            "--source",
            self.source,
            "--tail-file",
            self.tail_file,
            "--client-message-id",
            self.key,
            *(["--completion"] if self.completion else []),
        ]

    def json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "agent_id": self.agent_id,
                "source": self.source,
                "content": self.content,
                "tail_file": self.tail_file,
                "completion": self.completion,
                "command_exit": self.command_exit,
            }
        )


_ADAPTER = TypeAdapter(Completion)


def read_completion(text: str, marker: Path) -> Completion:
    # meiva:note
    # Reject malformed or misplaced intents without executing their contents.
    receipt = _ADAPTER.validate_json(text)
    if receipt.agent_id <= 0 or not 0 <= receipt.command_exit <= 255:
        raise ValueError("invalid agent id or command exit")
    if not re.fullmatch(r"(?:shell|watcher):\d+", receipt.source):
        raise ValueError("invalid completion source")
    if not receipt.content.strip():
        raise ValueError("empty completion content")
    if receipt.tail_file != str(marker.with_suffix(".log")):
        raise ValueError("completion log does not match receipt")
    return receipt
