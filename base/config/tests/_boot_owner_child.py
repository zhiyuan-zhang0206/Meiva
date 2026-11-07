from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# meiva:note
# The sentence only the delivery that ran can carry: the credential-free URL a refused delivery
# leaves behind reads as a generic "no database login was delivered" to anyone who re-derives the
# result from it, so a later owner that names the checkout captured the real reason (#6418).
RUNTIME_REFUSAL = "not the home's source checkout"

# meiva:note
# The pins have to land before any project import: `base.config`'s boot reads them while it
# imports, and the scenario decides which process this child is.
# argv: `<scenario> [<home> <other-home>]`; no scenario runs the read-only-owners case below.
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "read_only_owners"
if SCENARIO != "read_only_owners":
    os.environ["MEIVA_HOME"] = sys.argv[2]
    if SCENARIO == "dotenv":
        os.environ["MEIVA_HARNESS_HANDLE"] = "handle-for-the-dotenv-scenario"

from base.config import ConfigBoot, boot_state, settings  # noqa: E402


# meiva:note
# Two read-only owners capture one delivered environment: each keeps the values it was handed,
# builds its own eager chain, and leaves the process environment exactly as it found it.
def read_only_owners() -> None:
    before = dict(os.environ)
    owners: list[Any] = [ConfigBoot(), ConfigBoot()]
    for owner in owners:
        owner.read_process_environment()

    def build(owner: Any) -> Any:
        return owner.ensure_eager()

    with ThreadPoolExecutor(max_workers=2) as pool:
        models: list[Any] = list(pool.map(build, owners))
    assert dict(os.environ) == before, sorted(
        key for key in set(before) | set(os.environ) if before.get(key) != os.environ.get(key)
    )
    assert all(model.lm.llm_model == "delivered-model" for model in models)
    assert all(model.general.timezone == "Asia/Tokyo" for model in models)
    assert all(not model.general.message_timestamps for model in models)
    assert all(owner.field_explicitly_set("timezone") for owner in owners)
    assert all(
        model.lm.gemini_api_key.get_secret_value() == "delivered-provider-key" for model in models
    )
    complete = owners[0].complete_read_model()
    assert complete.lm.llm_model == "delivered-model"
    assert dict(os.environ) == before


def refusal(owner: ConfigBoot) -> str:
    boot = owner.env_boot
    return "" if boot is None else boot.db_authority_refusal or ""


def live_owner() -> ConfigBoot:
    owner = ConfigBoot()
    owner.read_process_environment()
    return owner


# meiva:note
# The facade prepared this process's owner and its delivery was refused, because the home's start
# intent admits another checkout. The owner a caller creates afterwards reads the same live
# environment, so it reports the same refusal -- the boot's, not a re-derivation from the URL.
def refused_home() -> None:
    assert boot_state()["prepared"]
    live = live_owner()
    assert RUNTIME_REFUSAL in refusal(live), live.env_boot
    assert refusal(live) == settings.env_boot.db_authority_refusal


# meiva:note
# A settings-lite process (`MEIVA_CONFIG_FETCH=skip`) prepares no process owner: this owner's own
# dotenv pass is the process's first delivery, and its refusal is the only precise one there is.
def dotenv() -> None:
    assert not boot_state()["prepared"]
    live = live_owner()
    assert RUNTIME_REFUSAL in refusal(live), live.env_boot


# meiva:note
# Neither of these may inherit the result above: a handed mapping is another process's launch, and
# a different home has a ledger of its own (the one this child is given holds none).
def owners_of_another_home() -> None:
    other = Path(sys.argv[3])
    handed = ConfigBoot(environment={"MEIVA_HOME": str(other)})
    handed.read_process_environment()
    assert refusal(handed) == "", handed.env_boot
    os.environ["MEIVA_HOME"] = str(other)
    assert refusal(live_owner()) == ""


SCENARIOS = {
    "read_only_owners": read_only_owners,
    "refused_home": refused_home,
    "dotenv": dotenv,
}

if __name__ == "__main__":
    SCENARIOS[SCENARIO]()
    if SCENARIO != "read_only_owners":
        owners_of_another_home()
