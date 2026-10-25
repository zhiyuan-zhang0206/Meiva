from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, TypeGuard

from base.host.private_storage import write_private_bytes
from cli.commands.extensions.external_skill_host.filesystem import (
    ClientConflictError,
    exists,
    manifest_digest,
    read_regular,
)

FORMAT = 6
_MARKER_NAME = ".meiva-managed.json"
SKILL_NAME = "operating-meiva-cluster"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9a-f]{32}$")


def ownership_marker(
    installation_id: str,
    generation_id: str,
    source_digest: str,
    *,
    skill_name: str = SKILL_NAME,
) -> bytes:
    return (
        json.dumps(
            {
                "format": FORMAT,
                "generation_id": generation_id,
                "installation_id": installation_id,
                "owner": "meiva",
                "skill": skill_name,
                "source_digest": source_digest,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def stage_manifest(source_manifest: list[dict[str, Any]], marker: bytes) -> list[dict[str, Any]]:
    return sorted(
        [
            *source_manifest,
            {
                "kind": "file",
                "mode": 0o600,
                "path": _MARKER_NAME,
                "sha256": hashlib.sha256(marker).hexdigest(),
            },
        ],
        key=lambda item: str(item["path"]),
    )


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_any_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def parse_record(path: Path) -> dict[str, Any] | None:
    if not exists(path):
        return None
    data, _ = read_regular(path, source=False)
    try:
        value: object = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClientConflictError("Meiva ownership ledger is invalid") from exc
    if not _is_any_dict(value):
        raise ClientConflictError("Meiva ownership ledger is invalid")
    return value


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _valid_manifest_item(item: dict[str, Any], paths: set[str]) -> bool:
    kind = item.get("kind")
    if kind == "file":
        required = {"kind", "mode", "path", "sha256"}
    elif kind == "directory":
        required = {"kind", "mode", "path"}
    else:
        return False
    path = item.get("path")
    mode = item.get("mode")
    return not (
        set(item) != required
        or not isinstance(path, str)
        or path in paths
        or (path != "." and (path.startswith("/") or ".." in Path(path).parts))
        or not isinstance(mode, int)
        or not 0 <= mode <= 0o7777
        or (kind == "file" and not _valid_digest(item.get("sha256")))
    )


def _valid_manifest(value: object) -> bool:
    if not _is_object_list(value):
        return False
    paths: set[str] = set()
    for item_value in value:
        if not _is_any_dict(item_value):
            return False
        if not _valid_manifest_item(item_value, paths):
            return False
        paths.add(item_value["path"])
    for item_value in value:
        if (
            _is_object_dict(item_value)
            and item_value.get("path") == "."
            and item_value.get("kind") == "directory"
        ):
            return True
    return False


def _expected_quarantine(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    source_path = Path(path)
    suffix = hashlib.sha256(path.encode()).hexdigest()[:16]
    return source_path.with_name(f".{source_path.name}.meiva-retained-{suffix}").as_posix()


def _valid_claim(
    claim: dict[str, object], expected_files: set[str], paths: set[str], quarantines: set[str]
) -> bool:
    path = claim.get("path")
    quarantine = claim.get("quarantine")
    return not (
        set(claim) != {"path", "quarantine", "state"}
        or not isinstance(path, str)
        or not isinstance(quarantine, str)
        or path not in expected_files
        or path in paths
        or quarantine in quarantines
        or quarantine != _expected_quarantine(path)
        or Path(quarantine).is_absolute()
        or ".." in Path(quarantine).parts
        or claim.get("state") not in {"source", "claiming", "quarantine", "retained"}
    )


def _valid_file_claims(value: object, manifest: object) -> bool:
    if not _is_object_list(value) or not _is_object_list(manifest):
        return False
    expected_files = {
        str(item["path"])
        for item in manifest
        if _is_object_dict(item) and item.get("kind") == "file"
    }
    paths: set[str] = set()
    quarantines: set[str] = set()
    for claim_value in value:
        if not _is_object_dict(claim_value):
            return False
        if not _valid_claim(claim_value, expected_files, paths, quarantines):
            return False
        paths.add(str(claim_value["path"]))
        quarantines.add(str(claim_value["quarantine"]))
    return paths == expected_files


def _all_claims_retained(value: object) -> bool:
    if not _is_object_list(value):
        return False
    for claim_value in value:
        if not _is_object_dict(claim_value):
            return False
        if claim_value.get("state") != "retained":
            return False
    return True


def _valid_cleanup_item(value: object, *, retained: bool) -> bool:
    if not _is_any_dict(value):
        return False
    allowed_locations = {"retained"} if retained else {"source", "claiming", "quarantine"}
    claims = value.get("file_claims")
    return (
        set(value) == {"file_claims", "kind", "location", "manifest", "path_generation_id"}
        and value["kind"] in {"prepared", "stage", "previous"}
        and value["location"] in allowed_locations
        and _valid_id(value["path_generation_id"])
        and _valid_manifest(value["manifest"])
        and _valid_file_claims(claims, value["manifest"])
        and (not retained or _all_claims_retained(claims))
    )


def _check_installed(installed_value: object) -> None:
    if not _is_any_dict(installed_value):
        raise ClientConflictError("Meiva ownership ledger is invalid")
    if (
        set(installed_value) != {"digest", "generation_id", "manifest", "source_digest"}
        or not all(_valid_digest(installed_value[key]) for key in ("digest", "source_digest"))
        or not _valid_id(installed_value["generation_id"])
        or not _valid_manifest(installed_value["manifest"])
        or manifest_digest(installed_value["manifest"]) != installed_value["digest"]
    ):
        raise ClientConflictError("Meiva ownership ledger is invalid")


def _check_transaction(transaction_value: object, *, has_installed: bool) -> None:
    if not _is_any_dict(transaction_value):
        raise ClientConflictError("Meiva ownership ledger is invalid")
    if (
        set(transaction_value)
        != {
            "claim_state",
            "expected_digest",
            "expected_manifest",
            "generation_id",
            "source_digest",
            "stage_state",
        }
        or not _valid_id(transaction_value["generation_id"])
        or not _valid_digest(transaction_value["source_digest"])
        or not _valid_digest(transaction_value["expected_digest"])
        or not _valid_manifest(transaction_value["expected_manifest"])
        or manifest_digest(transaction_value["expected_manifest"])
        != transaction_value["expected_digest"]
        or transaction_value["claim_state"] not in {"idle", "claiming", "claimed"}
        or transaction_value["stage_state"] not in {"preparing", "publishing", "published"}
        or (
            transaction_value["claim_state"] != "idle"
            and transaction_value["stage_state"] != "published"
        )
        or (transaction_value["claim_state"] != "idle" and not has_installed)
    ):
        raise ClientConflictError("Meiva ownership ledger is invalid")


def load_ledger(path: Path, client_key: str) -> dict[str, Any] | None:
    record = parse_record(path)
    if record is None:
        return None
    required = {
        "client",
        "format",
        "installation_id",
        "installed",
        "retained",
        "transaction",
        "garbage",
    }
    if set(record) != required or record["client"] != client_key or record["format"] != FORMAT:
        raise ClientConflictError("Meiva ownership ledger is invalid")
    if not _valid_id(record["installation_id"]):
        raise ClientConflictError("Meiva ownership ledger is invalid")
    installed_value: object = record["installed"]
    if installed_value is not None:
        _check_installed(installed_value)
    transaction_value: object = record["transaction"]
    if transaction_value is not None:
        _check_transaction(transaction_value, has_installed=installed_value is not None)
    for key, retained in (("garbage", False), ("retained", True)):
        items: object = record[key]
        if not _is_object_list(items):
            raise ClientConflictError("Meiva ownership ledger is invalid")
        for item_value in items:
            if not _valid_cleanup_item(item_value, retained=retained):
                raise ClientConflictError("Meiva ownership ledger is invalid")
    return record


def write_ledger(path: Path, ledger: dict[str, Any]) -> None:
    write_private_bytes(path, (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode())
