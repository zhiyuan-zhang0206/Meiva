# meiva:note
# What one start selected and where it runs: the facts every row of the dependency table reads.
#
# The step that composes the gate reads these from the converge context once (roles, services,
# the host's platform and uid, configuration, the install kind); everything below it takes the
# values, so a test states the selection it wants without building a host or a configuration.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PlaneFacts", "StartScope"]


# meiva:note
# The two facts of a gateway's data-plane configuration that decide which tools it needs.
@dataclass(frozen=True)
class PlaneFacts:
    is_remote: bool
    pgbouncer_enabled: bool


# meiva:note
# The selection of one start.
#
# `data_plane` is None where no data plane is configured (a host that is not a gateway).
# `macos` and `is_root` are the host platform and uid, read once. The memory pool consolidates
# through GitHub PRs on a split runner unless it is kept local, so `require_github_pr` and
# `memory_keep_local` decide whether `gh` is a start dependency. The boot unit is host-global:
# converge registers it only for the prod install (default home and a checkout that is not a
# dev worktree), which is `prod_install`.
@dataclass(frozen=True)
class StartScope:
    roles: frozenset[str] | None
    services: frozenset[str]
    data_plane: PlaneFacts | None
    macos: bool
    is_root: bool
    os_jobs: bool
    permissions_helper: bool
    require_github_pr: bool
    memory_keep_local: bool
    prod_install: bool

    # meiva:note
    # A gateway whose data plane lives on this host: the one that needs the database tools.
    @property
    def local_data_plane(self) -> bool:
        return (
            self.data_plane is not None
            and not self.data_plane.is_remote
            and bool(self.roles)
            and "gateway" in self.roles
        )

    # meiva:note
    # A local data plane that serves through PgBouncer.
    @property
    def pooled(self) -> bool:
        return (
            self.local_data_plane
            and self.data_plane is not None
            and self.data_plane.pgbouncer_enabled
        )

    # meiva:note
    # Whether a service that runs on Node (the frontend, the browser daemons) is selected.
    @property
    def node_selected(self) -> bool:
        return bool(self.services & {"frontend", "browser", "browser-mcp"})

    # meiva:note
    # OS jobs are registered through cron on Linux and launchd on macOS.
    @property
    def linux_os_jobs(self) -> bool:
        return self.os_jobs and not self.macos

    @property
    def macos_os_jobs(self) -> bool:
        return self.os_jobs and self.macos

    # meiva:note
    # The boot unit is the one host-global registrar (`converge_host` skips it for a dev
    # worktree), so systemd and the root step are required exactly where it runs.
    @property
    def boot_unit(self) -> bool:
        return self.linux_os_jobs and self.prod_install

    @property
    def boot_unit_through_sudo(self) -> bool:
        return self.boot_unit and not self.is_root

    # meiva:note
    # A split runner that consolidates memory through PRs cannot start without `gh`:
    # `ensure_github_pr` refuses to converge there. A gateway consolidates locally, and a
    # local-only pool never round-trips through GitHub at all.
    @property
    def github_pr_consolidation(self) -> bool:
        roles = self.roles or frozenset()
        return (
            "gateway" not in roles
            and bool(roles)
            and self.require_github_pr
            and not self.memory_keep_local
        )

    @property
    def macos_helper(self) -> bool:
        return self.macos and self.permissions_helper
