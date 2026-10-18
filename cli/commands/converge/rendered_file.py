from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from base.telemetry.converge_report import report_converge_overwrite, report_converge_preserve

LocalEditPolicy = Literal["preserve", "overwrite"]


# meiva:note
# The sidecar records the sha256 of what converge last wrote; a destination whose
# hash differs from it changed outside converge. `on_local_edit` names the owner:
# `preserve` keeps a surface a person may own and leaves the record untouched,
# `overwrite` restores a repository-owned surface and moves the record with it.
# A destination with no record is adopted under either policy, and either hit
# reports one telemetry event through converge_report.
# meiva:link cli/commands/converge/docs/rendered-files.meiva.okf.md
def write_rendered_guarded(
    path: Path,
    content: str,
    hashes_path: Path,
    key: str,
    *,
    surface: str,
    writer: Callable[[Path, str], None] | None = None,
    on_local_edit: LocalEditPolicy = "preserve",
) -> str | None:
    recorded: dict[str, str] = {}
    if hashes_path.exists():
        recorded = json.loads(hashes_path.read_text(encoding="utf-8"))
    drift: str | None = None
    if path.exists() and key in recorded:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current != recorded[key]:
            if on_local_edit == "preserve":
                report_converge_preserve(path=str(path), key=key, surface=surface)
                return (
                    f"{path} was modified locally; not overwritten "
                    f"(remove it and re-run converge to restore from the rendered output)"
                )
            report_converge_overwrite(path=str(path), key=key, surface=surface)
            drift = (
                f"{path} was modified locally; overwritten from the rendered source "
                "(this surface is repository-owned)"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    if writer is None:
        path.write_text(content, encoding="utf-8")
    else:
        writer(path, content)
    recorded[key] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    hashes_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
    return drift
