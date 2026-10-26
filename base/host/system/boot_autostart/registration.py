# meiva:link base/host/docs/boot-autostart.meiva.okf.md

from __future__ import annotations

from pathlib import Path

from loguru import logger

from base.host.system.boot_autostart.content import LABEL, launchd_plist_content
from base.host.system.boot_policy import BOOT_RETRY_INTERVAL_S, boot_start_arguments
from base.host.system.boot_unit import (
    SYSTEM_UNIT_DIR,
    SYSTEMD_RUNTIME_DIR,
    BootUnitContext,
    install,
    uninstall,
    unit_context,
)
from base.host.system.cron.mechanics import (
    bootout_launchd_job,
    owns_os_job_home,
    register_platform_job,
    skip_os_job,
    unregister_platform_job,
)
from base.host.system.hostport import Host
from base.host.system.job_readings import read_launchd_path, read_meiva_binary
from base.host.system.job_values import JobRoots, plist_path

# meiva:note
# launchctl answers at once; the bound only keeps a wedged launchd from stalling.
_LAUNCHCTL_TIMEOUT_S = 30.0


def register_macos(*, services: frozenset[str], host: Host, roots: JobRoots) -> int:
    # meiva:note
    # Write (or update) the autostart LaunchAgent plist. Idempotent: rewrites
    # only when the content changed, and never bootstraps (see base/host/docs/boot-autostart.meiva.okf.md).
    destination = plist_path(LABEL, user_home=roots.user_home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    meiva = read_meiva_binary(roots.repo, host=host)
    content = launchd_plist_content(
        arguments=(meiva, *boot_start_arguments(services)),
        retry_interval_seconds=BOOT_RETRY_INTERVAL_S,
        log_file=roots.home / "logs" / "autostart.log",
        environment_home=roots.home,
        path_env=read_launchd_path(meiva, user_home=roots.user_home, host=host),
    )
    if destination.exists() and destination.read_text() == content:
        # meiva:note
        # Already current: no write or reload.
        return 0
    destination.write_text(content)
    label = LABEL
    logger.info("Wrote autostart plist to {}", destination)
    # meiva:note
    # A job that is ALREADY loaded keeps its old definition until it is booted out:
    # `bootstrap` alone then fails on it, and the running session keeps starting the
    # previous service selection. Measured 2026-10-23 on macmini -- the plist named nine
    # `--only-service` flags while the loaded job still ran `meiva start --boot` with
    # none, so every attempt exited in argparse and `start-serving.json` never reached
    # `serving`. Printing only the first-load form is what hid it.
    print(  # noqa: T201
        f"  . '{label}' loads on next login/reboot.\n"
        f"    enable now (not loaded yet): launchctl bootstrap gui/{host.uid} {destination}\n"
        f"    already loaded (keeps its OLD definition until reloaded): "
        f"launchctl bootout gui/{host.uid}/{label} && "
        f"launchctl bootstrap gui/{host.uid} {destination}"
    )
    return 0


def _unregister_macos(*, host: Host, user_home: Path) -> int:
    destination = plist_path(LABEL, user_home=user_home)
    label = LABEL
    retired = bootout_launchd_job(label, host=host)
    if destination.exists():
        destination.unlink()
        logger.info("Removed autostart plist {}", destination)
    return 0 if retired else 1


def register_linux(
    *,
    context: BootUnitContext,
    host: Host,
    unit_dir: Path = SYSTEM_UNIT_DIR,
    runtime_dir: Path = SYSTEMD_RUNTIME_DIR,
) -> int:
    install(
        context=context,
        host=host,
        unit_dir=unit_dir,
        runtime_dir=runtime_dir,
    )
    return 0


def unregister_linux(
    *,
    host: Host,
    unit_dir: Path = SYSTEM_UNIT_DIR,
    runtime_dir: Path = SYSTEMD_RUNTIME_DIR,
) -> int:
    uninstall(host=host, unit_dir=unit_dir, runtime_dir=runtime_dir)
    return 0


def register_autostart(
    *,
    services: frozenset[str],
    enabled: bool,
    host: Host,
    roots: JobRoots,
) -> None:
    # meiva:note
    # Register the host's boot-time autostart job for exactly `services`.
    #
    # Platform-aware: dispatches to the macOS plist or the Linux boot unit. An
    # empty selection is refused: it would start nothing at boot, and an existing
    # boot job is never removed implicitly (`meiva cluster destroy` retires it).
    # Idempotent. On macOS the plist is written but not loaded into the running
    # session (see base/host/docs/boot-autostart.meiva.okf.md); it takes effect on the next reboot.
    # A no-op when ``enabled`` is off (the test suite) or when ``roots.home`` is not
    # the default home of ``roots.user_home`` (``owns_os_job_home``).
    #
    # Raises:
    #     RuntimeError: on registration failure.
    if not enabled:
        skip_os_job("autostart")
        return
    if not owns_os_job_home("autostart", roots=roots):
        return
    if not services:
        raise ValueError(
            "refusing to register a boot job that starts no service; name a selection with "
            "at least one service (the existing boot job is left as it is)"
        )
    register_platform_job(
        "autostart",
        macos=lambda: register_macos(services=services, host=host, roots=roots),
        linux=lambda: register_linux(
            context=unit_context(roots=roots, services=services, host=host), host=host
        ),
        host=host,
    )


def unregister_autostart(*, host: Host, roots: JobRoots) -> None:
    # meiva:note
    # Remove the host's boot job (systemd / launchd).
    #
    # Safe when none is registered, and a no-op when ``roots.home`` is not the default
    # home of ``roots.user_home`` (``owns_os_job_home``).
    if not owns_os_job_home("autostart", roots=roots):
        return
    unregister_platform_job(
        macos=lambda: _unregister_macos(host=host, user_home=roots.user_home),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )
