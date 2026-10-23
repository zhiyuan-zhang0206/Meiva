import sys

from cli.commands.management import config

if __name__ == "__main__":
    verb = sys.argv[1]
    try:
        if verb == "get":
            result = config.cmd_config_get(None, None, local=True)
        elif verb == "set":
            result = config.cmd_config_set(sys.argv[2:], None, local=True)
        elif verb == "gateway-base":
            print(config.gateway_base())
            result = 0
        elif verb == "put":
            config.put_config({"x": "y"}, None)
            result = 0
        elif verb == "remote-set":
            result = config.cmd_config_set(sys.argv[3:], sys.argv[2] or None)
        elif verb == "remote-unset":
            result = config.cmd_config_unset(sys.argv[3:], sys.argv[2] or None)
        elif verb == "remote-get":
            result = config.cmd_config_get(sys.argv[3], sys.argv[2] or None)
        elif verb == "remote-audit":
            from base.cluster.machine.boot import machine_from_boot
            from base.config import ConfigBoot

            boot = ConfigBoot()
            boot.read_process_environment()
            result = config.cmd_config_audit(
                int(sys.argv[3]), None, sys.argv[2] or None, identity=machine_from_boot(boot)
            )
        else:
            raise SystemExit(f"unknown verb {verb!r}")
    except config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        result = 1
    raise SystemExit(result)
