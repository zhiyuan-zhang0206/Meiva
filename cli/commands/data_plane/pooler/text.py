# meiva:note
# The lines the pooler's bring-up prints, as functions of values.
#
# The shell prints what these return, so a test asserts on the text without capturing a stream.
# Every failure line says what the operator can do next; the reachable-address lines say that
# a boot where the private network is not up yet is retried by running `meiva start` again.

from __future__ import annotations

from dataclasses import dataclass

from cli.commands.data_plane.pooler.convergence import BindContext

__all__ = [
    "PoolerText",
    "install_command",
    "not_installed_error",
    "not_installed_warning",
    "stale_pidfile",
]

_RETRY = "retry `meiva start --all-services` (or this unit's own service selection)"


# meiva:note
# How to install the binary: the brew formula on macOS; elsewhere Ubuntu's own archive carries
# an older PgBouncer, so the pinned build comes from the repository's PGDG provisioner.
def install_command(*, macos: bool, provision_script: str) -> str:
    return "brew install pgbouncer" if macos else f"sudo bash {provision_script}"


# meiva:note
# Enabled but not installed: fail fast, never fall back to direct. The operator asked for
# pooling.
def not_installed_error(binary: str, how: str) -> str:
    return (
        f"  ✗ MEIVA_PGBOUNCER_ENABLED is set but pgbouncer is not installed ({binary!r}). "
        f"Install it (`{how}`) and retry, or unset MEIVA_PGBOUNCER_ENABLED to run direct."
    )


# meiva:note
# The converge step only warns; bring-up refuses (`not_installed_error`).
def not_installed_warning(binary: str, how: str) -> str:
    return f"  ! pgbouncer enabled but not installed ({binary!r}) — run `{how}`"


# meiva:note
# A pidfile that names a live process which is not this home's pooler. That line is the one
# that makes a cross-home pidfile visible before it costs an outage.
def stale_pidfile(pidfile: str, pid: int) -> str:
    return (
        f"  · {pidfile} names pid {pid}, which is not this home's pooler "
        "(recycled pid) — not signalling it; removing the stale pidfile"
    )


# meiva:note
# The facts every line of one bring-up shares: the address consumers dial, how long the
# bring-up waited for it, the pooler's port and its log file.
@dataclass(frozen=True)
class PoolerText:
    host: str
    bind_wait_s: float
    listen_port: int
    logfile: str

    def bind_missing(self, context: BindContext) -> str:
        head = (
            f"  ✗ reachable bind address {self.host!r} is not assigned to any local "
            f"interface after {int(self.bind_wait_s)}s — "
        )
        if context is BindContext.RESTART_OF_DEGRADED:
            consequence = "the degraded pgbouncer cannot be restarted into a healthy double bind. "
        else:
            consequence = (
                "pgbouncer would silently degrade to loopback-only and every remote "
                "agent-runner would lose the pooled MEIVA_DB_URL. "
            )
        return (
            head
            + consequence
            + "On reboot this means the private network has not come up yet; "
            + f"{_RETRY} once it is."
        )

    def changed_restart(self) -> str:
        return (
            "  · pgbouncer configuration or userlist changed — restarting (a reload never revokes)"
        )

    def degraded_restart(self) -> str:
        return (
            f"  ✗ pgbouncer is NOT listening on the reachable address {self.host!r} — it "
            "degraded to loopback-only (task #1288) and remote agent-runners cannot reach "
            "the pooled MEIVA_DB_URL. Reload cannot re-bind it; restarting the pooler"
        )

    def stop_survived(self, pid: int) -> str:
        return (
            f"  ✗ could not stop the running pooler (pid {pid}) — it survived the "
            "graceful stop; custody retained, not starting a second pooler on the same port"
        )

    def admin_refused(self) -> str:
        return f"  ✗ pgbouncer (127.0.0.1:{self.listen_port}) refused its admin credential"

    def reloaded(self) -> str:
        return f"  ✓ pgbouncer already running (127.0.0.1:{self.listen_port}), reloaded"

    def launch_failed(self, returncode: int, stderr: str) -> str:
        return (
            f"  ✗ pgbouncer start failed (rc={returncode}); see {self.logfile}\n"
            f"    {stderr.strip()}"
        )

    def not_ready(self) -> str:
        return f"  ✗ pgbouncer did not become ready on :{self.listen_port}; see {self.logfile}"

    def degraded_after_launch(self) -> str:
        return (
            f"  ✗ pgbouncer started but is NOT listening on the reachable address "
            f"{self.host!r} — it degraded to loopback-only (see {self.logfile}). Every "
            "remote agent-runner's pooled MEIVA_DB_URL is dead. "
            "Retry `meiva start --all-services` (or this unit's own service selection) once "
            "the private network is up; the watchdog healthcheck will keep re-attempting."
        )

    def started(self) -> str:
        return f"  ✓ pgbouncer started (127.0.0.1:{self.listen_port}, transaction pooling)"
