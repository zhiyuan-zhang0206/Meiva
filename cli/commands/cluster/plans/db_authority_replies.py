# meiva:note
# What `meiva cluster db-authority issue-unit` and `install-unit` refuse and say.
#
# Values in, text out. The shells read the home, the roles and the sealed bundle's facts;
# this module decides which homes each verb refuses and the wording of every refusal and of
# the report that carries the transport key.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["IssuedReport", "failure_line", "install_refusal", "issue_refusal", "issued_text"]


# meiva:note
# One failed verb, in the form every refusal of these verbs takes.
def failure_line(verb: str, reason: str) -> str:
    return f"✗ meiva cluster db-authority {verb}: {reason}"


# meiva:note
# Why `issue-unit` refuses this home, or None when it may issue.
#
# The verb seals the active write generation, which only a gateway home with a local data
# plane keeps (a remote-managed plane has no write generation).
def issue_refusal(*, serves_gateway: bool, remote_plane: bool) -> str | None:
    if serves_gateway and not remote_plane:
        return None
    return failure_line(
        "issue-unit",
        "runs on a gateway home with a local data plane; a remote-managed plane has no write "
        "generation to issue",
    )


# meiva:note
# Why `install-unit` refuses a unit with these roles, or None when it may install.
#
# A gateway unit keeps its own write-generation ledger; the verb is for agent-runner units.
def install_refusal(roles: frozenset[str]) -> str | None:
    if "gateway" not in roles:
        return None
    return failure_line(
        "install-unit",
        "a gateway unit keeps its own write-generation ledger; install-unit is for "
        "agent-runner units",
    )


# meiva:note
# What the shell learned from sealing a bundle: the unit it is for, the write generation it
# carries, when it stops being installable (already formatted), where it was written and the
# transport key that opens it.
@dataclass(frozen=True)
class IssuedReport:
    unit: str
    generation: int
    expires: str
    target: str
    transport_key: str


# meiva:note
# The report of a sealed bundle. The transport key is shown here once and nowhere else.
def issued_text(report: IssuedReport) -> str:
    return (
        f"✓ bundle for {report.unit} (write generation {report.generation}, expires "
        f"{report.expires}) written to {report.target} (0600)\n"
        f"  transport key (shown once, carry it separately): {report.transport_key}\n"
        "  on the unit: export MEIVA_DB_CAPABILITY_KEY from a non-echoing prompt, then run its\n"
        "  checkout's `.venv/bin/meiva init --db-capability <bundle>` for a first join (init also "
        "takes --gateway-url, --machine-name and --machine-host), or `meiva cluster db-authority "
        "install-unit <bundle>` on an initialized unit; the unit never needs "
        "MEIVA_CLUSTER_SECRET; a bare `meiva` exists only once the unit's first start has linked it"
    )
