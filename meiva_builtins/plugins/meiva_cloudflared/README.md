# meiva_cloudflared

Builtin keepalive plugin that declares and supervises the `cloudflared` tunnel watchdog daemon (`session="cloudflared"`) through the plugin service declaration protocol (`services.py`).

## Overview

- **Manifest**: `meiva-plugin.json` (API v2, `category: "builtin"`)
- **Configuration**: `default_config.py` (`CloudflaredConfig` under `[meiva_cloudflared]` in `~/.meiva/config.toml`)
- **Service Declaration**: `services.py` -> `service_declaration/declaration.py` (`services(*, home, endpoints)`)
- **Watchdog Daemon**: `service_declaration/daemon.py` (`python -m meiva_builtins.plugins.meiva_cloudflared.service_declaration.daemon`)

## Lifecycle & Safety Guarantees

1. **Local Unix Socket Probe**: Listens on `~/.meiva/run/cloudflared-watchdog.sock` and responds to `services.supervision.healthchecks.protocol_probe.ping` with `{"id": 0, "ok": true}`.
2. **External Process Detection**: Before spawning a tunnel process, inspects running processes on the host for an active `cloudflared` tunnel (`tunnel` in cmdline). If a system-level `cloudflared` service (e.g. LaunchDaemon, launchd, systemd, Homebrew service) is already running, the watchdog monitors it without spawning a duplicate process.
3. **Graceful Missing-Config Idle**: If `cloudflared` is not installed or no tunnel config file (`~/.cloudflared/config.yml`, `/etc/cloudflared/config.yml`, or `cloudflared_config_path`) and no `cloudflared_tunnel` are present, the watchdog logs once and stays alive without crash-looping.
4. **Safe Shutdown (`meiva stop`)**: On `SIGTERM` / `SIGINT`, only a `cloudflared` child process spawned by this watchdog is terminated. External system-level `cloudflared` processes are never killed.
