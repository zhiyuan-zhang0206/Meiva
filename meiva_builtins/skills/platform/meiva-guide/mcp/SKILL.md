---
name: mcp
description: "Manages Meiva MCP connections and wrappers. Use when adding, configuring, inspecting, removing, or troubleshooting an MCP server."
---

# MCP — External Tool Integration

External tool servers (a browser, a language server, a vendor API) are wired in
as **MCP servers**. Once a server is configured on a machine, you call its tools
from code as `meiva.mcps.<server>.<tool>(**args)` — `help(meiva.mcps)` lists the
configured servers, `help(meiva.mcps.<server>)` lists a server's tools.

Configuration is **machine-level**: each box has its own server set (the merged
view of the machine config + any plugin-bundled `.mcp.json`).

This skill is the CLI reference. To go from "I want to reach tool X" to a
server that is installed and proven to work, read
`meiva.help(meiva.skills.meiva_guide.packages.install)` — it covers the major registries
and providers, approval of a specific process or remote connection, and a real
capability check. Its [source map](../packages/install/references/sources.md)
includes the official registry, hosted services, and community marketplaces.

## Add

Paste a server spec straight from a vendor README:

```bash
meiva mcp add <name> --json '{"command": "npx", "args": ["-y", "some-mcp-server"]}'
```

Or build it from flags instead of JSON:

```bash
meiva mcp add <name> --command npx --arg=-y --arg some-mcp-server --env KEY=VALUE
```

`--arg` takes one value; a value that starts with a dash needs the
`--arg=<value>` spelling (`--arg -y` reads as a new option).

`add` replaces an existing server of the same name. A newly added server
connects the next time you call one of its tools — no restart.

### Remote servers

Meiva also accepts a remote **Streamable HTTP** server object. Copy the endpoint
from the vendor's current documentation, then add it with `--json`:

```bash
meiva mcp add vendor --json '{"url":"https://vendor.example/mcp"}'
```

`url` and `command` are mutually exclusive. A remote entry can use static
`headers` or `"oauth": true`, not both. Follow the vendor's auth instructions
and the supported browser authorization flow; never put credentials into a
config overlay or a source repository. Check the running deployment's support and make
a real read-only tool call to verify access. An SSE-only endpoint is not a
Streamable HTTP endpoint.

## List / Remove / Enable / Disable

```bash
meiva mcp list             # the merged MCP server set on this machine
meiva mcp remove <name>    # remove a machine-config server
meiva mcp enable <name>    # toggle a server on this machine
meiva mcp disable <name>
```

`remove` deletes a server you added via machine config; `disable` keeps it but
stops it loading. A plugin-bundled server comes in with its plugin (see
[packages](../packages/SKILL.md)) rather than `meiva mcp add`.

## Cross-Machine Computer Use

`meiva.mcps.computer_use` drives the current machine by default (omit `host`) and can also route
actions to registered cluster desktops (`meiva://<machine>`) or external SSH-controlled desktops
configured in `$MEIVA_HOME/remote_hosts.toml` (`ssh://<alias>`).

1. Discover available targets, online status, and active lease holders first:
   ```bash
   meiva computer list          # human-readable table
   meiva computer list --json   # machine-readable JSON
   ```
2. Pass `host="<target>"` (or an unambiguous registered short name such as `"macmini"` or
   `"zzyasus"`) to any `computer_use` tool call — for example
   `meiva.mcps.computer_use.snapshot(host="ssh://zzyasus")`. Bare IP addresses and unregistered
   URLs are rejected.
3. Desktop leases default to `600s` (10 min, synchronized locally and on SSH targets) and remind
   you when `<= 180s` remain. Call `meiva.mcps.computer_use.acquire_control(host="<target>", ttl_seconds=1200)`
   to reserve or renew a longer window (30..3600s), and call
   `meiva.mcps.computer_use.release_control(host="<target>")` when done (or
   `meiva computer release --host <target>` as an operator kick).

## Cross-Machine Chrome

Use `meiva computer list --json` to discover registered targets. `browser_status`
reports the local or peer browser service's readiness. SSH rows report
`unprobed (SSH)` because discovery does not open tunnels.

```python
target = "meiva://macmini"  # or "ssh://zzyasus"
meiva.mcps.chrome.new_page(url="https://example.com", host=target)
meiva.mcps.chrome.take_snapshot(host=target)
```

Omit `host` for local Chrome. Pass the same target to each remote call and
open a tab before acting on it. The agent-host supplies a caller credential to
hosted execute-code and harness sessions.

- The cluster browser daemon records Tab ownership and a TTL that survives
  runner restarts. The TTL sweep also handles Tabs left by caller disconnects.
- SSH binds Tab IDs to one connection. During normal teardown it requests
  closure of the Tabs it owns. Abrupt disconnects or daemon exits can leave
  Tabs open on the target, and Chrome may retain the last Tab. SSH Tabs have
  no remote TTL. Open a new page after a disconnect or browser reconnect.
  Named `isolatedContext` is unavailable on SSH targets.

Configure `ssh_host`, `os`, and optionally `browser_port` in the host entry
under `[hosts.<alias>]` in `$MEIVA_HOME/remote_hosts.toml`. Chrome must already
listen on the target's loopback interface (port 9222 by default). SSH uses the
configured login and host key. Run Chrome under that login for file uploads.

Use an agent workspace path for file uploads and saved screenshots. Remote
snapshots return inline text only, capped at 8 MiB before forwarding. A call
transfers up to 8 MiB of files. Submit an attached file within ten minutes on
a cluster target or before the SSH client connection ends. Run
`help(meiva.mcps.chrome)` for the available tools.

## When to Fix Behavior in a Wrapper Instead

If a first-party MCP server (one Meiva ships with) has a behavior bug, the fix is
a thin wrapper MCP server that passes the upstream through and intercepts only
the broken call — not a special-case in the generic MCP call path. Isolate the
fix so it does not pollute the shared channel.
