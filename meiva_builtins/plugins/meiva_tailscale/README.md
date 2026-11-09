# meiva_tailscale

Builtin keepalive plugin that declares and supervises the `tailscale` mesh VPN watchdog daemon (`session="tailscale"`) through the plugin service declaration protocol (`services.py`).

## Overview

- **Manifest**: `meiva-plugin.json` (API v2, `category: "builtin"`)
- **Configuration**: `default_config.py` (`TailscaleConfig` under `[meiva_tailscale]` in `~/.meiva/config.toml`)
- **Service Declaration**: `services.py` -> `service_declaration/declaration.py` (`services(*, home, endpoints)`)
- **Watchdog Daemon**: `service_declaration/daemon.py` (`python -m meiva_builtins.plugins.meiva_tailscale.service_declaration.daemon`)

## Lifecycle & Safety Guarantees

1. **Local Unix Socket Probe**: Listens on `~/.meiva/run/tailscale-watchdog.sock` and responds to `services.supervision.healthchecks.protocol_probe.ping` with `{"id": 0, "ok": true}`.
2. **Periodic Status Check**: Runs `tailscale status --json` every `tailscale_check_interval_seconds`:
   - If `BackendState == "Running"`, the mesh interface is healthy.
   - On macOS, if the CLI cannot reach the local daemon and `/Applications/Tailscale.app` exists, runs `open -g -a Tailscale` in the background without stealing focus.
   - If `BackendState != "Running"` and `tailscale_auto_up` is enabled, runs `tailscale up` with any configured `tailscale_up_args`.
3. **Graceful Missing-Binary Idle**: If `tailscale` is not installed on the host, the watchdog logs once and stays alive without crash-looping.
4. **Safe Shutdown (`meiva stop`)**: On `SIGTERM` / `SIGINT`, only the keepalive watchdog stops and cleans up its Unix socket and pidfile. It never runs `tailscale down` or terminates `tailscaled` / `Tailscale.app`.
