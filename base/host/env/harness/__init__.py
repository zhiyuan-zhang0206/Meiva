# meiva:note
# External harness catalog specifications, binary resolution, and convergence engine.
# meiva:link base/lm/harness/docs/cli-convergence.meiva.okf.md

from __future__ import annotations

from base.host.env.harness.catalog import (
    CATALOG as CATALOG,
)
from base.host.env.harness.catalog import (
    CLI_NAMES as CLI_NAMES,
)
from base.host.env.harness.catalog import (
    ConvergenceReport as ConvergenceReport,
)
from base.host.env.harness.catalog import (
    HarnessSpec as HarnessSpec,
)
from base.host.env.harness.catalog import (
    HarnessUpdateOutcome as HarnessUpdateOutcome,
)
from base.host.env.harness.catalog import (
    Installer as Installer,
)
from base.host.env.harness.catalog import (
    InstallKind as InstallKind,
)
from base.host.env.harness.catalog import (
    OutcomeStatus as OutcomeStatus,
)
from base.host.env.harness.catalog import (
    Rotation as Rotation,
)
from base.host.env.harness.catalog import (
    Store as Store,
)
from base.host.env.harness.catalog import (
    UpdateSource as UpdateSource,
)
from base.host.env.harness.catalog import (
    credential_risk as credential_risk,
)
from base.host.env.harness.catalog import (
    install_command as install_command,
)
from base.host.env.harness.catalog import (
    needs_cli_update as needs_cli_update,
)
from base.host.env.harness.catalog import (
    parse_cli_version as parse_cli_version,
)
from base.host.env.harness.catalog import (
    spec as spec,
)
from base.host.env.harness.catalog import (
    staged_install_command as staged_install_command,
)
from base.host.env.harness.convergence import (
    CommandResult as CommandResult,
)
from base.host.env.harness.convergence import (
    ShellRunner as ShellRunner,
)
from base.host.env.harness.convergence import (
    converge_local_harness_clis as converge_local_harness_clis,
)

__all__ = [
    "CATALOG",
    "CLI_NAMES",
    "CommandResult",
    "ConvergenceReport",
    "HarnessSpec",
    "HarnessUpdateOutcome",
    "InstallKind",
    "Installer",
    "OutcomeStatus",
    "Rotation",
    "ShellRunner",
    "Store",
    "UpdateSource",
    "converge_local_harness_clis",
    "credential_risk",
    "install_command",
    "needs_cli_update",
    "parse_cli_version",
    "spec",
    "staged_install_command",
]
