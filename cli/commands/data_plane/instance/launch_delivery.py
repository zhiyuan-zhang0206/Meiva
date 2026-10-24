# meiva:note
# What a service launch is delivered about the database and the API, and how.
#
# `LaunchDelivery` is the process's facts a launch reads, taken once at the start entry: the
# home, whether the data plane is remote-managed, the process environment and the home's
# `.env` values. `db_delivery_kind` and `api_delivery_kind` decide which delivery a service
# class gets; the shell in `bringup.py` produces the delivered values.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "Delivery",
    "LaunchDelivery",
    "api_delivery_kind",
    "db_delivery_kind",
]


# meiva:note
# The facts one launch reads from the process, read once and handed down.
#
# `environment` is the launch process's environment and `env_file` the home's `.env` values;
# together they say whether this unit serves the gateway and carry the runner login snapshot.
@dataclass(frozen=True)
class LaunchDelivery:
    home: Path
    remote_data_plane: bool
    environment: Mapping[str, str]
    env_file: Mapping[str, str]


# meiva:note
# Where a service's credential comes from.
class Delivery(Enum):
    # meiva:note
    # The unit capability installed on a pure agent-runner (runner class only).
    UNIT_CAPABILITY = "unit capability"
    # meiva:note
    # Nothing is delivered: a remote-managed plane keeps no write generations.
    NONE = "none"
    # meiva:note
    # The home's active write generation, for this class.
    GENERATION = "generation"


# meiva:note
# The database delivery of one service of class `cls`.
#
# A local plane delivers the active write generation's login; a pure agent-runner (its config
# source is not this home) delivers its installed unit capability, and only the runner class
# exists there; a remote-managed gateway plane delivers nothing, its agents keep the provider
# projection.
def db_delivery_kind(cls: str, *, config_source_is_local: bool, remote_plane: bool) -> Delivery:
    if not config_source_is_local:
        if cls != "runner":
            raise RuntimeError(f"a pure agent-runner cannot launch a {cls}-class database service")
        return Delivery.UNIT_CAPABILITY
    return Delivery.NONE if remote_plane else Delivery.GENERATION


# meiva:note
# The API-token delivery of one service of class `cls`.
#
# The cluster's API always authenticates, so every service is delivered a token to present as
# its bearer: a pure agent-runner its capability's runner token, the gateway home the active
# generation's `cls` token. A remote-managed plane keeps no write generations and delivers
# nothing; its gateway-local services present the human secret.
def api_delivery_kind(cls: str, *, config_source_is_local: bool, remote_plane: bool) -> Delivery:
    if not config_source_is_local:
        if cls != "runner":
            raise RuntimeError(f"a pure agent-runner cannot launch a {cls}-class service")
        return Delivery.UNIT_CAPABILITY
    return Delivery.NONE if remote_plane else Delivery.GENERATION
