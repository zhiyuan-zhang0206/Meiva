from __future__ import annotations

import argparse
from pathlib import Path

from base.native_process.os_platform import file_lock
from base.paths.private_dir import ensure_private_dir
from cli import init_intent, start_identity
from cli.start_runtime import HomeSite
from cli.startup.service_path import CallerHost


# meiva:note
# A caller that says nothing about its host unless a test names it.
def caller_host(
    *,
    service_path: str | None = None,
    path: str = "",
    virtual_env: str | None = None,
    prefix_venv: str | None = None,
    occupied_ports: frozenset[int] = frozenset(),
) -> CallerHost:
    return CallerHost(service_path, path, virtual_env, prefix_venv, occupied_ports)


# meiva:note
# Publish a home's identity as `meiva init` does, without its verdict output.
def prepare_init_identity(args: argparse.Namespace, site: HomeSite, caller: CallerHost) -> None:
    ensure_private_dir(site.home)
    with file_lock(site.home / "start-intent.lock", timeout_s=30):
        init_intent.initialize_home(args, site, caller)


# meiva:note
# `home` as `meiva init` leaves a remote runner, which `meiva start`'s gate admits.
#
# A runner records no ports and creates no data plane, so nothing here touches the host.
def initialized_runner_home(home: Path, checkout: Path) -> Path:
    checkout.mkdir(exist_ok=True)
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            home,
            checkout,
            frozenset({"agent-runner"}),
            {
                "MEIVA_MACHINE_NAME": "runner",
                "MEIVA_GATEWAY_URL": "http://127.0.0.1:1",
                "MEIVA_SERVICE_PATH": str(checkout.parent / "tools"),
            },
        )
    )
    return home


# meiva:note
# A home and a checkout under `root`; the checkout exists and is empty.
def site_in(root: Path) -> HomeSite:
    checkout = root / "checkout"
    checkout.mkdir(exist_ok=True)
    return HomeSite(root / "home", checkout)


# meiva:note
# Make the next `.env` write of `home` fail on disk, as a power loss between the
# intent and the `.env` would: its lock file is a directory.
def break_env_publication(home: Path) -> None:
    home.mkdir(mode=0o700, exist_ok=True)
    (home / ".env.lock").mkdir()


def repair_env_publication(home: Path) -> None:
    (home / ".env.lock").rmdir()
