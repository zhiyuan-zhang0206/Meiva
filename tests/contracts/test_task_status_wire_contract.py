import json
from pathlib import Path

from base.agents.tasks.status import TaskStatus


def test_task_status_enum_is_single_sourced_across_wire_and_frontend() -> None:
    root = Path(__file__).resolve().parents[2]
    openapi = json.loads((root / "ui" / "web" / "openapi.json").read_text(encoding="utf-8"))
    enum = openapi["components"]["schemas"]["TaskStatus"]["enum"]
    assert set(enum) == {s.value for s in TaskStatus}

    types_ts = (root / "ui" / "web" / "src" / "lib" / "contracts" / "types.ts").read_text(
        encoding="utf-8"
    )
    assert 'export type TaskStatus = Schemas["TaskStatus"];' in types_ts, (
        "lib/contracts/types.ts must alias the generated TaskStatus schema (single source), "
        "not restate a status union"
    )
