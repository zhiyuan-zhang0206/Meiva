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
)
from base.host.system.cron import mechanics
from base.host.system.cron.mechanics import (
    bootout_launchd_job,
    meiva_binary_path,
    owns_os_job_home,
    owns_os_jobs,
    register_platform_job,
    skip_os_job,
    unregister_platform_job,
)
from base.host.system.hostport import Host
from base.paths import meiva_home

# meiva:note
# launchctl answers at once; the bound only keeps a wedged launchd from stalling.
_LAUNCHCTL_TIMEOUT_S = 30.0


def _autostart_plist_path() -> Path:
    return mechanics.user_plist_path(LABEL)


def register_macos(*, services: frozenset[str], host: Host) -> int:
    # meiva:note
    # Write (or update) the autostart LaunchAgent plist. Idempotent: rewrites
    # only when the content changed, and never bootstraps (see base/host/docs/boot-autostart.meiva.okf.md).
    plist_path = _autostart_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    meiva = meiva_binary_path(host=host)
    log_file = meiva_home() / "logs" / "autostart.log"
    arguments = (meiva, *boot_start_arguments(services))
    environment_home = Path(mechanics.job_home())
    path_env = mechanics.launchd_path_env(host=host)
    content = launchd_plist_content(
        arguments=arguments,
        retry_interval_seconds=BOOT_RETRY_INTERVAL_S,
        log_file=log_file,
        environment_home=environment_home,
        path_env=path_env,
    )
    if plist_path.exists() and plist_path.read_text() == content:
        # meiva:note
        # Already current: no write or reload.
        return 0
    plist_path.write_text(content)
    label = LABEL
    logger.info("Wrote autostart plist to {}", plist_path)
    # meiva:note
    # A job that is ALREADY loaded keeps its old definition until it is booted out:
    # `bootstrap` alone then fails on it, and the running session keeps starting the
    # previous service selection. Measured 2026-10-23 on macmini -- the plist named nine
    # `--only-service` flags while the loaded job still ran `meiva start --boot` with
    # none, so every attempt exited in argparse and `start-serving.json` never reached
    # `serving`. Printing only the first-load form is what hid it.
    print(  # noqa: T201
        f"  . '{label}' loads on next login/reboot.\n"
        f"    enable now (not loaded yet): launchctl bootstrap gui/{host.uid} {plist_path}\n"
        f"    already loaded (keeps its OLD definition until reloaded): "
        f"launchctl bootout gui/{host.uid}/{label} && "
        f"launchctl bootstrap gui/{host.uid} {plist_path}"
    )
    return 0


def _unregister_macos(*, host: Host) -> int:
    plist_path = _autostart_plist_path()
    label = LABEL
    retired = bootout_launchd_job(label, host=host)
    if plist_path.exists():
        plist_path.unlink()
        logger.info("Removed autostart plist {}", plist_path)
    return 0 if retired else 1


def register_linux(
    *,
    services: frozenset[str],
    host: Host,
    context: BootUnitContext | None = None,
    unit_dir: Path = SYSTEM_UNIT_DIR,
    runtime_dir: Path = SYSTEMD_RUNTIME_DIR,
) -> int:
    install(
        services=services,
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
    home: Path | None = None,
) -> None:
    # meiva:note
    # Register the host's boot-time autostart job for exactly `services`.
    #
    # Platform-aware: dispatches to the macOS plist or the Linux boot unit. An
    # empty selection is refused: it would start nothing at boot, and an existing
    # boot job is never removed implicitly (`meiva cluster destroy` retires it).
    # Idempotent. On macOS the plist is written but not loaded into the running
    # session (see base/host/docs/boot-autostart.meiva.okf.md); it takes effect on the next reboot.
    # A no-op when ``enabled`` is off (the test suite) or when this
    # process's home is not the default home (``owns_os_jobs``).
    #
    # Raises:
    #     RuntimeError: on registration failure.
    if not enabled:
        skip_os_job("autostart")
        return
    owns = owns_os_jobs("autostart") if home is None else owns_os_job_home("autostart", home=home)
    if not owns:
        return
    if not services:
        raise ValueError(
            "refusing to register a boot job that starts no service; name a selection with "
            "at least one service (the existing boot job is left as it is)"
        )
    register_platform_job(
        "autostart",
        macos=lambda: register_macos(services=services, host=host),
        linux=lambda: register_linux(services=services, host=host),
        host=host,
    )


def unregister_autostart(*, host: Host) -> None:
    # meiva:note
    # Remove the host's boot job (systemd / launchd).
    #
    # Safe when none is registered, and a no-op when this process's home is not the
    # default home (``owns_os_jobs``).
    if not owns_os_jobs("autostart"):
        return
    unregister_platform_job(
        macos=lambda: _unregister_macos(host=host),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )
