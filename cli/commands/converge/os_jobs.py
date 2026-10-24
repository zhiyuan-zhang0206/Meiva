"""Converge steps that register this host's OS-level scheduled jobs.

Six jobs, one concept — everything Meiva asks the platform scheduler (launchd /
crontab) to run on its behalf:

- **health probe** — periodic cluster health check reporting observations
  (gateway); it takes no repair action.
- **boot autostart** — brings the whole cluster back after a reboot (prod only).
- **logs maintenance** — daily copytruncate rotation followed by tiered retention.
- **packages refresh** — the content channel's recurring pass (skills fast lane).
- **PR flow** — the daily merge-pipeline sampler, credential-gated to the
  production home that can reach GitHub and Trunk (task #2139).
- **WAL-G tick** — the daily physical backup, present exactly while
  `MEIVA_WALG_CONFIG_FILE` is set.

They share a shape worth keeping together: each is idempotent, each delegates the
platform branching to a ``base.os_*`` module, and each fails the converge loudly
rather than leaving the cluster silently unsupervised.
"""

from __future__ import annotations

from base.host.system.job_values import JobRoots
from cli.commands.converge.spec import ConvergeCtx


def _job_roots(ctx: ConvergeCtx) -> JobRoots:
    return JobRoots(home=ctx.meiva_home, repo=ctx.repo, user_home=ctx.session.user_home)


def ensure_health_probe_cron(ctx: ConvergeCtx) -> None:
    """Register the OS cron job for the cluster health probe.

    Only runs on gateway hosts (roles gated). Delegates to `base.host.system.cron`.
    The primary registration path is now in the gateway lifespan
    (`gateway/app.py`); this converge step is a belt-and-suspenders fallback
    that runs before the gateway process starts. Idempotent."""
    from base.host.system.cron import register_os_cron

    register_os_cron(enabled=ctx.read_config().view.general.os_jobs_enabled, host=ctx.host)
    # On failure the exception propagates so converge fails fast.


def ensure_logs_maintenance(ctx: ConvergeCtx) -> None:
    """Register daily rotation followed by retention."""
    from base.host.system.logs_job import register_logs_job

    register_logs_job(
        enabled=ctx.read_config().view.general.os_jobs_enabled, roots=_job_roots(ctx), host=ctx.host
    )


# meiva:note
# Register the host's content-refresh tick only from the owning home. Resolve the channel switch
# after global/home admission, and the interval only for an enabled channel. The registrar keeps
# its own ownership check at the physical-write boundary; its command rechecks both switches.
def ensure_packages_refresh_job(ctx: ConvergeCtx) -> None:
    from base.host.system import cron
    from base.host.system.packages_content import RefreshSchedule
    from base.host.system.packages_job import register_packages_job

    view = ctx.read_config().view
    jobs_enabled = view.general.os_jobs_enabled
    if jobs_enabled and not cron.owns_os_job_home("packages refresh", home=ctx.meiva_home):
        return
    refresh_enabled = view.packages.refresh_enabled if jobs_enabled else False
    register_packages_job(
        enabled=jobs_enabled,
        schedule=RefreshSchedule(
            refresh_enabled=refresh_enabled,
            tick_seconds=view.packages.refresh_tick_seconds if refresh_enabled else None,
        )
        if jobs_enabled
        else None,
        roots=_job_roots(ctx),
        host=ctx.host,
    )
    # A registration failure propagates so converge fails fast.


def ensure_pr_flow_job(ctx: ConvergeCtx) -> None:
    """Register the daily PR-flow sampler job (task #2139).

    The gate lives in `base.host.system.pr_flow_job.register_pr_flow_job`: the job is
    registered only on a production home whose machine holds the sampler's
    credentials (`gh` on PATH + a Trunk API token) — in the fleet, macmini.
    Every other unit skips with the reason logged, so converge output explains
    the absence. Idempotent."""
    from base.cluster.derive import production_identity
    from base.host.system.pr_flow_job import register_pr_flow_job

    register_pr_flow_job(
        enabled=ctx.read_config().view.general.os_jobs_enabled,
        production=production_identity(machine=ctx.machine, home=ctx.meiva_home),
        roots=_job_roots(ctx),
        host=ctx.host,
    )


def ensure_walg_job(ctx: ConvergeCtx) -> None:
    """Keep the daily WAL-G tick registered exactly while WAL-G is switched on.

    Key set: register (idempotent). Key unset: remove a job a previous
    configuration left behind, so turning WAL-G off leaves no schedule that runs
    a command which now does nothing. Both directions only act in the default
    home (`owns_os_jobs`), and registration is a no-op where
    `MEIVA_OS_JOBS_ENABLED` is off. Delegates to `base.host.system.walg_job`."""
    from base.host.system import cron
    from base.host.system.walg_job import register_walg_job, unregister_walg_job

    if not cron.owns_os_job_home("WAL-G tick", home=ctx.meiva_home):
        return
    if ctx.read_config().view.walg.walg_config_file is not None:
        view = ctx.read_config().view
        jobs_enabled = view.general.os_jobs_enabled
        register_walg_job(
            enabled=jobs_enabled,
            backup_hour=view.services.backup_hour if jobs_enabled else None,
            roots=_job_roots(ctx),
            host=ctx.host,
        )
    else:
        unregister_walg_job(host=ctx.host)


def ensure_cluster_autostart(ctx: ConvergeCtx) -> None:
    # meiva:note
    # Register the boot-time autostart job so a machine reboot brings this
    # cluster's gateway / agents / daemons back up without a manual `meiva start`
    # (macOS launchd RunAtLoad / Linux systemd).
    #
    # host_global-gated to the prod install, so a dev worktree cluster never
    # registers autostart (its plist would dangle once the worktree is removed).
    # The boot job's command line names the services this start selected
    # (`--only-service` each), so a boot start never infers a selection.
    # Delegates to `base.host.system.autostart`. Idempotent.
    from base.host.system.autostart import register_autostart

    register_autostart(
        services=ctx.services,
        enabled=ctx.read_config().view.general.os_jobs_enabled,
        host=ctx.host,
    )
    # On failure the exception propagates so converge fails fast.
