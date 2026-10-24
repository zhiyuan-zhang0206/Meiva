# meiva:note
# A fresh interpreter for the WAL-G tests that need their own process environment.
#
# `env` prints the environment `walg_env()` hands wal-g; `profile <walg path>` prints what a
# `ConfigBoot` of this process's profile says about the `walg` domain.

import json
import sys
from pathlib import Path

from base.config import ConfigBoot
from services.backup.walg import runner
from services.backup.walg.config import configured_path


def _has_walg(boot: ConfigBoot) -> bool:
    try:
        _ = boot.view.walg
    except AttributeError:
        return False
    return True


def _profile(path: Path) -> dict[str, object]:
    boot = ConfigBoot()
    facts: dict[str, object] = {
        "declared": boot.view.has_domain("walg"),
        "readable": _has_walg(boot),
    }
    if facts["declared"]:
        boot.set_field("walg_config_file", path)
        other = ConfigBoot()
        other.ensure_eager()
        facts["owner_reader"] = str(
            configured_path(path_reader=lambda: boot.view.walg.walg_config_file)
        )
        facts["other_declared"] = other.view.has_domain("walg")
    else:
        boot.ensure_eager()
        facts["declared_after_eager"] = boot.view.has_domain("walg")
        facts["readable_after_eager"] = _has_walg(boot)
    return facts


if __name__ == "__main__":
    if sys.argv[1] == "env":
        sys.stdout.write(json.dumps(runner.walg_env()))
    elif sys.argv[1] == "profile":
        sys.stdout.write(json.dumps(_profile(Path(sys.argv[2]))))
    else:
        raise SystemExit(f"unknown mode {sys.argv[1]!r}")
