# meiva:note
# meiva.understand batch (concurrent) mode: validation, order preservation, concurrency caps, and
# error propagation. The scripted text model answers each request from the request itself.

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage

from meiva.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from meiva.tests.namespaces.understand.provider_support import UnderstandHarness
from meiva.tests.namespaces.understand.provider_support import (
    understand_harness as understand_harness,
)
from meiva.understand import understand


def _blocks(messages: list[BaseMessage]) -> list[dict[str, str]]:
    [message] = messages
    assert isinstance(message.content, list)
    return [block for block in message.content if isinstance(block, dict)]


def _echo_prompt(messages: list[BaseMessage]) -> str:
    return f"answer to: {_blocks(messages)[1]['text']}"


# meiva:note
# Counts the model calls in flight, and the most there ever were at once.
def _prompt_and_material(messages: list[BaseMessage]) -> str:
    blocks = _blocks(messages)
    return f"{blocks[1]['text']}: {blocks[0]['text']}"


class _Peak:
    def __init__(self, hold: float) -> None:
        self._hold = hold
        self._lock = threading.Lock()
        self._inflight = 0
        self.peak = 0

    def __call__(self, messages: list[BaseMessage]) -> str:
        with self._lock:
            self._inflight += 1
            self.peak = max(self.peak, self._inflight)
        time.sleep(self._hold)
        with self._lock:
            self._inflight -= 1
        return "ok"


def test_batch_targets_validation() -> None:
    with pytest.raises(TypeError, match="takes a list of target dicts"):
        understand("not a list")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a dict"):
        understand(["not a dict"])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="missing required key 'prompt'"):
        understand([{"paths": ["x.txt"]}])
    with pytest.raises(ValueError, match="exactly one of 'text' / 'paths'"):
        understand([{"prompt": "p", "paths": ["x.txt"], "text": "y"}])
    with pytest.raises(ValueError, match="exactly one of 'text' / 'paths'"):
        understand([{"prompt": "p"}])


def test_batch_text_concurrent_preserves_order(understand_harness: UnderstandHarness) -> None:
    understand_harness.text.replies = (_echo_prompt,)

    results = understand_harness.understand(
        [
            {"prompt": "first question", "text": "material A"},
            {"prompt": "second question", "text": "material B"},
            {"prompt": "third question", "text": "material C"},
        ]
    )

    assert results == [
        "answer to: first question",
        "answer to: second question",
        "answer to: third question",
    ]


def test_batch_mixed_paths_and_text(understand_harness: UnderstandHarness, tmp_path: Path) -> None:
    f = tmp_path / "readme.md"
    f.write_text("# Project\nDescription here.", encoding="utf-8")
    understand_harness.text.replies = (_prompt_and_material,)
    targets: list[dict[str, str | list[str]]] = [
        {"prompt": "summarize", "text": "inline text"},
        {"prompt": "extract", "paths": [str(f)]},
        {"prompt": "analyze", "text": "more inline"},
    ]

    assert understand_harness.understand(targets) == [
        "summarize: inline text",
        "extract: # Project\nDescription here.",
        "analyze: more inline",
    ]


def test_batch_default_concurrency_uses_the_shared_ceiling(
    understand_harness: UnderstandHarness,
) -> None:
    n = DEFAULT_BATCH_MAX_CONCURRENT + 2
    peak = _Peak(hold=0.05)
    understand_harness.text.replies = (peak,)

    results = understand_harness.understand(
        [{"prompt": f"q{i}", "text": f"m{i}"} for i in range(n)]
    )

    assert results == ["ok"] * n
    assert peak.peak <= DEFAULT_BATCH_MAX_CONCURRENT


def test_max_concurrent_validation(understand_harness: UnderstandHarness) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        understand_harness.understand([{"prompt": "x", "text": "y"}], max_concurrent=0)
    with pytest.raises(ValueError, match="at least 1"):
        understand_harness.understand([{"prompt": "x", "text": "y"}], max_concurrent=-2)
    with pytest.raises(TypeError, match="int or None"):
        understand_harness.understand(
            [{"prompt": "x", "text": "y"}],
            max_concurrent="4",  # type: ignore[arg-type]
        )
    assert understand_harness.text.requests == []


def test_max_concurrent_caps_inflight_calls(understand_harness: UnderstandHarness) -> None:
    peak = _Peak(hold=0.05)
    understand_harness.text.replies = (peak,)

    results = understand_harness.understand(
        [{"prompt": f"q{i}", "text": f"m{i}"} for i in range(6)], max_concurrent=2
    )

    assert results == ["ok"] * 6
    assert peak.peak == 2


def test_max_concurrent_one_serializes(understand_harness: UnderstandHarness) -> None:
    peak = _Peak(hold=0.02)
    understand_harness.text.replies = (peak,)

    results = understand_harness.understand(
        [{"prompt": f"q{i}", "text": f"m{i}"} for i in range(3)], max_concurrent=1
    )

    assert results == ["ok"] * 3
    assert peak.peak == 1


def test_batch_error_propagates(understand_harness: UnderstandHarness, tmp_path: Path) -> None:
    targets: list[dict[str, str | list[str]]] = [
        {"prompt": "q1", "text": "ok"},
        {"prompt": "q2", "paths": [str(tmp_path / "nonexistent.txt")]},
        {"prompt": "q3", "text": "also ok"},
    ]

    with pytest.raises(FileNotFoundError):
        understand_harness.understand(targets)
