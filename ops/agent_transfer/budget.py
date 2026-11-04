# meiva:note
# The budget one workspace transfer must fit: its two ceilings as one value, the framed-stream bound
# they imply, and the refusals that name them.
#
# The numbers themselves live only in `settings.transfer` (`base/config/domains/transfer.py`); this
# module keeps no copy of them, so the ceiling a deployment configures reaches every check and every
# refusal that describes it.
# meiva:link base/config/docs/workspace-transfer-limits.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass

from ops.agent_transfer.errors import WorkspaceTransferTooLargeError

# meiva:note
# Room the framed stream takes above a transfer's byte ceiling: the magic, the manifest length
# prefix and the JSON manifest itself — one entry per file, directory and symlink.
BUNDLE_STREAM_SLACK_BYTES = 24 * 1024 * 1024

# meiva:note
# The settings a refusal names, so an operator reads the repair from the error itself. A test pins
# both spellings to the `settings.transfer` fields they describe, because this module is pure and
# cannot read the config to check them at run time.
WORKSPACE_TRANSFER_FILES_SETTING = (
    "settings.transfer.max_workspace_files (MEIVA_TRANSFER_MAX_WORKSPACE_FILES)"
)
WORKSPACE_TRANSFER_BYTES_SETTING = (
    "settings.transfer.max_workspace_bytes (MEIVA_TRANSFER_MAX_WORKSPACE_BYTES)"
)


# meiva:note
# The two ceilings one transfer enforces, as one value every enforcing caller hands down:
# `max_files` bounds entries (files, directories and symlinks), `max_bytes` bounds the original file
# bytes the workspace holds.
@dataclass(frozen=True, slots=True)
class WorkspaceTransferLimits:
    max_files: int
    max_bytes: int


# meiva:note
# The framed-stream ceiling a byte ceiling implies: the bundle is the byte ceiling plus the framing
# and manifest above it, and both ends refuse a stream past it.
def max_bundle_stream_bytes(limits: WorkspaceTransferLimits) -> int:
    return limits.max_bytes + BUNDLE_STREAM_SLACK_BYTES


# meiva:note
# Refuse a framed stream that runs past the ceiling its byte ceiling implies. Every end of a transfer
# reads the same number: the source while it frames the bundle, the gateway while it holds it, and
# the target before it extracts anything.
def check_bundle_stream_size(size: int, *, limits: WorkspaceTransferLimits) -> None:
    stream_limit = max_bundle_stream_bytes(limits)
    if size > stream_limit:
        raise WorkspaceTransferTooLargeError(
            f"workspace bundle stream is {size} bytes, over the {stream_limit}-byte ceiling "
            f"({BUNDLE_STREAM_SLACK_BYTES} bytes of framing above the {limits.max_bytes}-byte "
            f"transfer ceiling, {WORKSPACE_TRANSFER_BYTES_SETTING})"
        )


# meiva:note
# The one over-budget refusal for a complete measurement: both axes, each actual number beside its
# effective ceiling, and the setting that moves it.
def over_budget_error(
    limits: WorkspaceTransferLimits, *, entries: int, total_bytes: int
) -> WorkspaceTransferTooLargeError:
    return WorkspaceTransferTooLargeError(
        f"workspace does not fit a transfer: {entries} entries against the "
        f"{limits.max_files}-entry ceiling ({WORKSPACE_TRANSFER_FILES_SETTING}) and "
        f"{total_bytes} bytes against the {limits.max_bytes}-byte ceiling "
        f"({WORKSPACE_TRANSFER_BYTES_SETTING})"
    )


# meiva:note
# The same refusal for a walk that stopped at the first ceiling it crossed, before it had measured
# the other axis: the numbers report what the walk saw, which is a floor on the workspace's size
# rather than its total.
def over_budget_during_walk_error(
    limits: WorkspaceTransferLimits, *, entries: int, bytes_read: int
) -> WorkspaceTransferTooLargeError:
    return WorkspaceTransferTooLargeError(
        f"workspace is over the transfer ceiling and was refused mid-walk: {entries} entries "
        f"seen so far against the {limits.max_files}-entry ceiling "
        f"({WORKSPACE_TRANSFER_FILES_SETTING}), {bytes_read} bytes read so far against the "
        f"{limits.max_bytes}-byte ceiling ({WORKSPACE_TRANSFER_BYTES_SETTING})"
    )
