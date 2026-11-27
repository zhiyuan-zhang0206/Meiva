# meiva:link cli/fleet_update/docs/structure.md

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Literal, TypeGuard

from ..untyped import object_of

_PREFIX = "FLEET_DOMAIN="
_VERSION = 2
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _mapping(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


@dataclass(frozen=True)
class DomainReceipt:
    target: str
    scope: Literal["local", "runner"]
    config: str
    schema: str
    applied: str
    components: dict[str, str]

    def encode(self) -> str:
        return json.dumps({"version": _VERSION, **asdict(self)}, sort_keys=True)

    def line(self) -> str:
        return _PREFIX + self.encode()


def _hex_field(value: dict[str, object], key: str, pattern: re.Pattern[str]) -> str:
    digest = value.get(key)
    if not isinstance(digest, str) or not pattern.fullmatch(digest):
        raise ValueError(f"invalid receipt {key}")
    return digest


def _component_checks(components: object) -> dict[str, str]:
    if not _mapping(components) or not {"config", "schema", "checkpoint"} <= components.keys():
        raise ValueError("receipt omitted component checks")
    checks: dict[str, str] = {}
    for component, state in components.items():
        if not component or not isinstance(state, str) or not state:
            raise ValueError("invalid receipt component check")
        checks[component] = state
    return checks


def decode_receipt(raw: str) -> DomainReceipt:
    value = object_of(json.loads, raw)
    if not _mapping(value) or type(value.get("version")) is not int or value["version"] != _VERSION:
        raise ValueError("unsupported fleet domain receipt version")
    scope = value.get("scope")
    if scope not in ("local", "runner"):
        raise ValueError("invalid receipt config scope")
    return DomainReceipt(
        _hex_field(value, "target", _SHA),
        "runner" if scope == "runner" else "local",
        _hex_field(value, "config", _DIGEST),
        _hex_field(value, "schema", _DIGEST),
        _hex_field(value, "applied", _DIGEST),
        _component_checks(value.get("components")),
    )


def receipt_from_output(output: str, *, target: str | None) -> DomainReceipt:
    packets = [
        line.removeprefix(_PREFIX) for line in output.splitlines() if line.startswith(_PREFIX)
    ]
    if len(packets) != 1:
        raise ValueError("expected one versioned fleet domain receipt")
    receipt = decode_receipt(packets[0])
    if target is not None and receipt.target != target:
        raise ValueError(f"receipt target SHA {receipt.target} differs from {target}")
    return receipt


def require_process_proof(output: str, *, target: str | None) -> None:
    proofs = [line for line in output.splitlines() if line.startswith("FLEET_SUPERVISION=")]
    if len(proofs) != 1:
        raise ValueError("expected one meiva-root process proof")
    fields = proofs[0].removeprefix("FLEET_SUPERVISION=").split()
    if (
        len(fields) != 5
        or not _SHA.fullmatch(fields[0])
        or not _DIGEST.fullmatch(fields[1])
        or not fields[2].isascii()
        or not fields[2].isdecimal()
        or fields[3:] != ["running", "units"]
    ):
        raise ValueError("invalid meiva-root process proof")
    if target is not None and fields[0] != target:
        raise ValueError(f"meiva-root proof is for {fields[0]}, expected {target}")
