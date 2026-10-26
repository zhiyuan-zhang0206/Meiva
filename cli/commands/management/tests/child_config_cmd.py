import os
import sys
from pathlib import Path

from cli.commands.management import config

PROCESS = config.ConfigProcess(environ=os.environ, home=Path(os.environ["MEIVA_HOME"]))

if __name__ == "__main__":
    verb = sys.argv[1]
    try:
        if verb == "get":
            result = config.cmd_config_get(None, None, process=PROCESS, local=True)
        elif verb == "set":
            result = config.cmd_config_set(sys.argv[2:], None, process=PROCESS, local=True)
        elif verb == "gateway-base":
            print(config.gateway_base(PROCESS))
            result = 0
        elif verb == "put":
            config.put_config({"x": "y"}, None, PROCESS)
            result = 0
        elif verb == "remote-set":
            result = config.cmd_config_set(sys.argv[3:], sys.argv[2] or None, process=PROCESS)
        elif verb == "remote-unset":
            result = config.cmd_config_unset(sys.argv[3:], sys.argv[2] or None, process=PROCESS)
        elif verb == "remote-get":
            result = config.cmd_config_get(sys.argv[3], sys.argv[2] or None, process=PROCESS)
        elif verb == "remote-audit":
            from base.cluster.machine.configured import ConfiguredMachineIdentity
            from base.config import ConfigBoot

            boot = ConfigBoot()
            boot.read_process_environment()
            result = config.cmd_config_audit(
                int(sys.argv[3]),
                None,
                sys.argv[2] or None,
                identity=ConfiguredMachineIdentity(boot),
                process=PROCESS,
            )
        else:
            raise SystemExit(f"unknown verb {verb!r}")
    except config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        result = 1
    raise SystemExit(result)
