# meiva:note
# The four ways a workspace transfer refuses: it conflicts with the agent's state, it is too large for
# the ceilings it must fit, its bundle is corrupt, or (as the base of all three) it fails for a reason
# the caller reports verbatim. `reason` is the token a caller turns into its own status code, so the
# class — not a message match — is what a refusal is judged by.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations


class WorkspaceTransferError(ValueError):
    reason: str = "transfer_error"


class WorkspaceTransferConflictError(WorkspaceTransferError):
    reason: str = "transfer_conflict"


class WorkspaceTransferTooLargeError(WorkspaceTransferError):
    reason: str = "transfer_too_large"


class WorkspaceTransferCorruptError(WorkspaceTransferError):
    reason: str = "transfer_corrupt"
