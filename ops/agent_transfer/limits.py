# meiva:note
# The `settings.transfer` ceilings as the one value a transfer's enforcing ends hand down.
#
# A composition root reads them once (its config authority) and passes them into every check it
# runs, so no check reaches for process settings itself and a test drives a ceiling the same way it
# drives a workspace. The pair is cluster-pinned, so the source that packs, the gateway that holds
# the bundle and the target that extracts all read one number: a transfer never packs what its
# receiver would refuse.
# meiva:link base/config/docs/workspace-transfer-limits.meiva.okf.md

from __future__ import annotations

from base.config.service_read import ConfigAuthority
from ops.agent_transfer.budget import WorkspaceTransferLimits


# meiva:note
# The pair an ops or gateway root derives from its read model: the complete model, not this
# process's profile, so the ceiling does not depend on which process kind happens to enforce it.
def workspace_transfer_limits_from_read_model(
    model: ConfigAuthority,
) -> WorkspaceTransferLimits:
    return WorkspaceTransferLimits(
        max_files=model.service_field_value("max_workspace_files"),
        max_bytes=model.service_field_value("max_workspace_bytes"),
    )
