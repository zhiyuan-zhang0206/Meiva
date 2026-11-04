# meiva:note
# The two ceilings a whole-agent workspace transfer enforces — entries and original (uncompressed)
# bytes — as `settings.transfer` fields, cluster-pinned and consumed by both process kinds that
# enforce them: the source runner packs under them, and the gateway plus the target runner refuse a
# bundle above them. The defaults are the constants the feature landed with; no commit records why
# those numbers were chosen (the feature's own description still said 2 GiB / 100,000), so they are
# a conservative pair an operator can now raise without a code change.
# meiva:link base/config/docs/workspace-transfer-limits.meiva.okf.md
from __future__ import annotations

from pydantic import Field
from pydantic.config import JsonDict

from base.config.base import EnvSettings

_TRANSFER_FIELD: JsonDict = {
    "restart_required": "all",
    "writable": True,
    "sensitive": False,
    "scope": "cluster-pinned",
}

_BYTES_RATIONALE = (
    "Original file bytes, not the compressed bundle: a transfer is refused on what the workspace "
    "holds. The bundle is an uncompressed tar stream buffered whole in the source runner's memory "
    "and again in the gateway, so this is also the per-request memory bound of one transfer."
)

_FILES_RATIONALE = (
    "Entries, not files alone: directories and symlinks count, because the bundle carries each of "
    "them as a tar member and each is verified on the far side. The 2026-11-03 refusal was this "
    "axis — 595,471 entries against 20,000 — while the same workspace compressed to 78 MB."
)


class TransferSettings(EnvSettings):
    max_workspace_bytes: int = Field(
        default=256 * 1024 * 1024,
        gt=0,
        alias="MEIVA_TRANSFER_MAX_WORKSPACE_BYTES",
        description=(
            f"Largest total of original file bytes one workspace transfer may carry. "
            f"{_BYTES_RATIONALE} Raised with caution: past a few GiB the bundle stops fitting the "
            f"memory of the process kinds that hold it. The framed stream ceiling is this value "
            f"plus 24 MiB of manifest slack."
        ),
        json_schema_extra=_TRANSFER_FIELD,
    )

    max_workspace_files: int = Field(
        default=20_000,
        gt=0,
        alias="MEIVA_TRANSFER_MAX_WORKSPACE_FILES",
        description=(
            "Most entries (files, directories and symlinks) one workspace transfer may carry. "
            f"{_FILES_RATIONALE}"
        ),
        json_schema_extra=_TRANSFER_FIELD,
    )
