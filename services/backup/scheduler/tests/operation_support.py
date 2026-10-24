from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from services.backup.scheduler.operation.staging import OperationKind

STUB_WORKER = "services.backup.scheduler.tests.stub_worker"


def operation_kind(tmp_path: Path, **changes: Any) -> OperationKind:
    return OperationKind("test", tmp_path / "controls", **changes)


async def until(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert path.exists(), f"{path.name} was never written"
