# meiva:note
# Short-lived child process for test_telemetry_otlp_exit_delivery.

from __future__ import annotations

import sys
from functools import partial

from base import telemetry
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home


def main() -> None:
    marker = sys.argv[1]
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(process="exit-seam-probe", version=version.get)
    config = ConfigBoot()
    config.read_process_environment()
    pipeline = telemetry.build_pipeline(
        database=partial(Database.from_settings, gate=gate),
        machine=ConfiguredMachineIdentity(config),
        home=meiva_home(),
    )
    telemetry.init_telemetry(home=meiva_home(), process="exit-seam-probe", pipeline=pipeline)
    telemetry.emit("log", "log", attributes={"seq": "early", "marker": marker})
    telemetry.sync()
    telemetry.emit("log", "log", attributes={"seq": "tail", "marker": marker})


if __name__ == "__main__":
    main()
