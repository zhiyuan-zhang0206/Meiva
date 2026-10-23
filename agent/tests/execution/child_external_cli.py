import os
import sys
from pathlib import Path
from typing import Any

import meiva
from agent.state import build_agent_state, plugin_state_schema
from base.packages.plugins.loading.registry import build_registry
from meiva.impersonation import attachment as attachment_module

SAMPLE = sys.argv[1]
LEASE: dict[str, Any] = {
    "id": "lease",
    "session_id": 0,
    "agent_id": 424242,
    "machine": "external-cli-probe",
    "status": "active",
    "delta_version": 0,
    "applied_version": 0,
    "plugin_delta": [],
    "event_delivery_protocol_version": None,
}


def require_active(_db: Any, _lease_id: Any, *, local_machine: str) -> dict[str, Any]:
    assert local_machine == "external-cli-probe"
    return dict(LEASE)


def load_snapshot(_agent_id: Any, **_kwargs: Any) -> tuple[Any, dict[str, Any], None]:
    state: Any = build_agent_state(build_registry(validate_state=plugin_state_schema))()
    state.meiva_code__cwd = str(Path(SAMPLE).parent)
    return state, {}, None


os.environ["MEIVA_MACHINE_NAME"] = "external-cli-probe"

with attachment_module.Attachment(
    "lease", lease_reader=require_active, snapshot_loader=load_snapshot
):
    assert meiva.files.read(SAMPLE) == "tail event"
