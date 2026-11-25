# meiva:link base/agents/epochs/docs/epochs.md

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from base.agents.epochs.model import RecordExtent
from base.agents.history.transcript_timeline.record.harness import (
    compressed_file,
    record_file,
    session_file,
)
from base.api_contracts.agent_contract import reads_timeline_from_record
from base.lm.harness.registry import FileSessionRegistry


def record_extent(
    registry: FileSessionRegistry,
    environ: Mapping[str, str],
    agent_id: int,
    harness: str,
) -> RecordExtent | None:
    if not reads_timeline_from_record(harness):
        return None
    record = registry.load(agent_id, harness=harness)
    if record is None:
        return None
    path = session_file(harness, environ, Path(record.cwd), record.session_id)
    if path is not None:
        return RecordExtent(record.session_id, record_file(harness, path).size())
    if compressed_file(harness, environ, record.session_id) is not None:
        return None
    return RecordExtent(record.session_id, 0)
