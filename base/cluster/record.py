"""A cluster home's own record: the ports and data-plane host it was born with.

The record lives in the home it describes, as the `record` of the home's start
intent (`$MEIVA_HOME/start-intent.json`, written by `cli.start_identity` before
any effect). A home describes only itself: nothing here lists, looks up or
reserves against another cluster. A unit without the gateway capability owns no
data plane, so its intent carries no record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TypeGuard

from base.cluster.ports import ClusterPorts
from base.host.boundary.fields import str_field
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

INTENT_NAME = "start-intent.json"


@dataclass(frozen=True)
class ClusterRecord:
    ports: ClusterPorts
    gateway_home: str
    created_at: str
    # The host this cluster's data-plane URLs are DERIVED at (birth); empty =
    # loopback (127.0.0.1), the single-box posture. Stored on the record because
    # derivation happens at birth, before the home's `.env` exists; the birth path
    # snapshots it from `MEIVA_DATA_PLANE_HOST` — see `cli.start_identity` and
    # `base.cluster.derive.per_cluster_base_urls`. External data plane: Task #1752.
    data_plane_host: str = ""


# meiva:note
# Every port the record carries is a whole number. Completeness against `FIXED_PORTS` is not
# checked here: `cli.start_identity` refuses an incomplete record when it writes one, and a reader
# of an older or partial record asks only for the ports it needs.
def _is_cluster_ports(value: dict[str, object]) -> TypeGuard[ClusterPorts]:
    return all(isinstance(port, int) and not isinstance(port, bool) for port in value.values())


# meiva:note
# `home` is the resolved home the caller read; a `~` spelling stays literal, so no
# reader answers "which home does this `~` name" from the user database.
def get_record(home: Path) -> ClusterRecord | None:
    """This home's record, read from its own start intent.

    None when the home has no start intent (not born) or its intent carries no
    record (a unit without the gateway capability). Nothing outside the home is
    consulted.
    """
    path = Path(home) / INTENT_NAME
    if not path.exists():
        return None
    intent = object_of(json.loads, path.read_text())
    if not is_object_dict(intent):
        raise TypeError(f"{path} is not a JSON object")
    record = intent["record"]
    if record is None:
        return None
    if not is_object_dict(record):
        raise TypeError(f"the record in {path} is not a JSON object")
    unexpected = sorted(record.keys() - {field.name for field in fields(ClusterRecord)})
    if unexpected:
        raise TypeError(f"the record in {path} has unexpected keys {unexpected}")
    ports = record["ports"]
    if not is_object_dict(ports) or not _is_cluster_ports(ports):
        raise TypeError(f"the record in {path} has no port table of whole numbers")
    return ClusterRecord(
        ports=ports,
        gateway_home=str_field(record, "gateway_home"),
        created_at=str_field(record, "created_at"),
        data_plane_host=str_field(record, "data_plane_host") if "data_plane_host" in record else "",
    )
