from __future__ import annotations

from base.log import logger
from base.telemetry.emitter import emit


# meiva:note
# One event per preserve hit: the renderer kept local content instead of the
# render, so the drift stays visible without reading converge output. `path` is
# the preserved destination, `key` the hash-sidecar key (or managed name),
# `surface` the renderer.
# meiva:link cli/commands/converge/docs/rendered-files.meiva.okf.md
def report_converge_preserve(*, path: str, key: str, surface: str) -> None:
    try:
        emit(
            "telemetry",
            "converge_file_preserved",
            level="warning",
            source="converge",
            attributes={"path": path, "key": key, "surface": surface},
        )
    except Exception:
        # `_no_emitter`: the failure may be the telemetry pipeline itself; its report must not
        # re-enter it. Converge must continue.
        logger.bind(_no_emitter=True).opt(exception=True).warning(
            "converge_file_preserved event for {path} was not emitted; the preserve is "
            "visible only in converge output",
            path=path,
        )


# meiva:note
# The mirror of report_converge_preserve for a surface the repository owns
# outright: the destination drifted from the record and converge restored it.
def report_converge_overwrite(*, path: str, key: str, surface: str) -> None:
    try:
        emit(
            "telemetry",
            "converge_file_overwritten",
            level="warning",
            source="converge",
            attributes={"path": path, "key": key, "surface": surface},
        )
    except Exception:
        logger.bind(_no_emitter=True).opt(exception=True).warning(
            "converge_file_overwritten event for {path} was not emitted; the restore is "
            "visible only in converge output",
            path=path,
        )
