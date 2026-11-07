# meiva:note
# The startup delivery results a boot-lite owner reads, and the dotenv pass a harness launch needs
# before anything can capture its environment.
#
# A refusal is a fact about one (process, home) pair, and only the owner that ran the delivery can
# say why it was refused: the credential-free URL a refused delivery leaves behind (`MEIVA_DB_URL`
# with no password) reads the same whether the ledger refused this process's runtime, the launcher
# delivered no login, or the home holds no active generation. Re-deriving the result from that URL
# through `read_database_delivery` therefore replaced "this process runs <cwd>, not the home's
# source checkout <source>" with "no database login was delivered by this process's startup entry",
# which hid the cause of a whole failed external-harness attach (#6418).
# meiva:link base/cluster/authority/docs/wiring.meiva.okf.md

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from base.host.env.bootstrap import EnvBootResult
from base.host.env.dotenv_boot import load_meiva_env, read_database_delivery


# meiva:note
# The owner a process booted with -- `base.config`'s import tail prepares exactly one: the home it
# delivered for and the result it delivered. A later owner reading that same live environment
# captures those instead of re-deriving a result from what the boot left behind.
class ProcessBoot(Protocol):
    @property
    def home(self) -> Path | None: ...

    @property
    def env_boot(self) -> EnvBootResult | None: ...


# meiva:note
# The dotenv pass a harness-launched process needs before anything captures its environment.
#
# A harness handle with no database URL is the one launch that must load the home's `.env` and
# deliver the operator login here, because nothing has yet. The result is returned rather than
# dropped: when the delivery is refused, this pass is what knows why.
def harness_dotenv_pass(*, home: Path) -> EnvBootResult | None:
    if not os.environ.get("MEIVA_HARNESS_HANDLE"):
        return None
    for key in [
        key for key, value in os.environ.items() if key.startswith("MEIVA_") and value == ""
    ]:
        os.environ.pop(key, None)
    if os.environ.get("MEIVA_DB_URL"):
        return None
    return load_meiva_env(os.environ, home=home)


# meiva:note
# The delivery result an owner captures: this process's own for this home, else the dotenv pass's
# refusal, else what the environment's ledger says about the URL it holds. `process` is the owner
# the process booted with, or None for an owner that reads a handed environment -- another
# process's launch says nothing about this one.
def captured_env_boot(
    environment: Mapping[str, str],
    *,
    home: Path,
    process: ProcessBoot | None,
    dotenv_result: EnvBootResult | None,
) -> EnvBootResult:
    if process is not None:
        delivered_home, delivered = process.home, process.env_boot
        if (
            delivered is not None
            and delivered_home is not None
            and delivered_home.resolve() == home.resolve()
        ):
            return delivered
    if dotenv_result is not None and dotenv_result.db_authority_refusal is not None:
        return dotenv_result
    return read_database_delivery(environment, home=home)
