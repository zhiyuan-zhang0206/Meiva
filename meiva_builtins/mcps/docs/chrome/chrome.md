# Chrome MCP — native Chrome + chrome-devtools-mcp upstream

## What It Is
`meiva.mcps.chrome` lets agents drive a logged-in shared headed Chrome (navigate / click / fill forms / screenshot / DOM / console). It is **not** a standalone MCP server, but rather wraps the upstream `chrome-devtools-mcp` (Chrome DevTools MCP, npm package) into a two-stage bridge of "one upstream, many agents reuse".

## Two and a Half Layers
```
agent's shared MCP daemon (meiva.mcps._daemon, in-daemon BrowserLineSession)
      ←Unix socket→ mcp_daemon (per-machine shared, browser-mcp session)
      ←→ chrome-devtools-mcp upstream ←CDP→ shared headed Chrome
```

- **chrome-devtools-mcp upstream** (Chrome DevTools MCP): the npm package that actually speaks CDP to drive Chrome. Its collector subscribes to targets across the **entire browser**, so each upstream buffers network/console traffic for all tabs.
- **mcp_daemon** (`services.desktop.browser.mcp_daemon`, ServiceSpec session `browser-mcp`): a per-machine **single** upstream, shared across all agents via a Unix socket. Replaces the old per-agent upstream — N browser agents used to each spawn an upstream, each buffering all browser traffic, making it the #1 memory hog on agent-runner; sharing to 1 means each tab's traffic is received only once.
- **in-daemon line client** (`meiva/mcps/_browser.py:connect_browser_direct`, wired via `"shared": "browser"` in `mcps/chrome/.mcp.json`): the agent's shared MCP daemon dials the browser-mcp service's line protocol directly, **no child process at all**. This replaced the per-agent `services.desktop.browser.mcp_wrapper` stdio bridge (~63MB RSS per agent). Each agent connection keeps its own socket, so the daemon's per-connection page affinity still isolates agents; tool lists pass through verbatim, so upstream upgrades (new/renamed tools) are automatically reflected (the one exception is `renew_page`, a daemon-owned tool appended after the passthrough — see "Page Lifetime (TTL)"). The client self-heals a desynced stream (a response id that does not match the request, or a corrupt line): it rebuilds the socket and restarts the id counter, so a lost response line fails one call but never bricks the connection. The wrapper remains in the tree only as the declared command for hosts running older daemons.

## Two Invariants (daemon side, to make a single upstream safe for multiple clients)
- **Serial lock**: machine-wide one-at-a-time browser operations, multi-step sequences do not interleave (operator chose serial over parallel).
- **Per-connection page affinity**: chrome-devtools-mcp has only one global "current page", but each client has its own tab; the daemon re-selects the connection's page before forwarding page-scoped calls, so A's `click` won't land on B's tab; connections without a page are not forwarded to the global selected page (could be someone else's tab), instead they cold-start (navigate → new_page) or return a no-page error.

## Page Lifetime (TTL)
Every page this stack **creates** — an explicit `new_page`, or the auto-created page on a page-less first navigate — carries a hard TTL deadline (`MEIVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`, 24h default), kept in `<home>/run/browser-pages.json` (one record per CDP target id, wall-clock epoch seconds) and enforced by the expiry sweep (`services/desktop/browser/page_lifecycle.py:reap_expired_pages`), the only safety net behind the deterministic release on agent exit. Activity never extends it; a page in use across days is bounded by its TTL (reclaimed within a sweep interval). An agent that dies without reaching its exit hook (SIGKILL, OOM, force-terminate), or a dev server that dies under a live agent, leaves its tab in the shared Chrome until that deadline (24h by default).

- **Renewal** — `renew_page`, the daemon-owned tool: moves the caller's deadline to `now + ttl` (default: the configured TTL; max 24h per call), mirroring the shell `renew()` ruling. A passed deadline is not renewable — the sweep owns the page.
- **Scoping** — cleanup acts on pages with a TTL record, written when the stack creates a page (an isolated-context page too). The `Meiva #<id>` tab group only checks that the user has not taken the page: a page that was in its owner's group and is no longer is left open (its record is dropped), and a page in an agent group with no record is neither adopted nor closed (except one restart inheritance and a one-time migration, see the browser tab-groups doc). Before every close the page is re-checked, an unreadable Chrome closes nothing, and the browser's last page is never closed. Selecting a user tab grants affinity only: release clears it without closing, and `renew_page` refuses. Explicit `close_page` is unchanged.
- **Connection scope** — page ids are per upstream process (`chrome-devtools-mcp` renumbers from 1 on every restart), so affinity is not carried across a daemon restart and the agent rebuilds through the cold-start paths (or `list_pages` + `select_page`). The TTL records are carried: release, expiry and `renew_page` keep working for pages created before the restart (`renew_page` matches the selected page to the agent's own recorded pages by URL and refuses an ambiguous match). Pages are closed by CDP target id, not through `close_page`, only when two consecutive sweeps found them due, and never in the first sweep after a daemon start.
- **Expiry surface** — no invented "page expired" error: the next page-scoped call takes the existing no-page path; a fresh navigate cold-starts a new page with its own TTL.
- **Cleanup** — a `close_page`, release, or expiry-sweep close drops the page's TTL record with it. A close counts only when the target is gone; otherwise the record stays (original deadline) and the next pass retries.

## Configuration Layer and Startup Form
chrome is the **builtin (native) layer** in the four-layer MCP config merge (builtin < plugin < installed < machine, see [MCP Server System](../../../../meiva/mcps/docs/mcps.md)): its definition ships with the code in `mcps/chrome/.mcp.json` and does not enter the install registry (`meiva mcp install` installs self-contained packages at the installed layer, e.g., x).

Launch is **relative path direct launch** for the declared command, never `uv run` (the latter is a persistent wrapper process, pure overhead under per-agent spawn): command writes a relative interpreter `.venv/bin/python`, and `meiva/mcps/config.py:server_cwd()` determines the spawn cwd per effective layer — built-in **pins to repo root**, so relative paths resolve to the repo venv. Since `"shared": "browser"` the daemon never spawns this command at all — it dials the browser-mcp service socket directly; the command line is retained for compatibility and as documentation of the underlying bridge.

## Gating / Lifecycle
- Gated together with the browser service (`MEIVA_BROWSER_ENABLED` + display / Chrome / npx capability) — see [Browser service](../../../../services/docs/agent_runner_side/browser/browser/browser.md).
- The `.mcp.json` declares `requires: {display}`; the daemon's own gate (`services/supervision/roster/spec.py:gate_reason` → `browser_incapability()`) is the same as the browser service's.
- The browser-mcp daemon establishes one upstream per process. Upstream death logs an error and exits after cleanup; there is no internal reconnect loop. The root retains its bounded process restart policy. Reattachment can lose page affinity; continuity is not guaranteed.

## Key Dependencies
- [MCP Server System](../../../../meiva/mcps/docs/mcps.md) — overall MCP call mechanism (`meiva.mcps.<server>.<tool>`)
- [Browser — Headed Chrome Reuse Service](../../../../services/docs/agent_runner_side/browser/browser/browser.md) — service/lifecycle for shared Chrome startup + upstream daemon (agent-runner side)

## Entry Points
- `mcps/chrome/.mcp.json` — chrome MCP server definition (`"shared": "browser"` — daemon dials the browser-mcp service directly; declared command = relative direct launch `.venv/bin/python -m services.desktop.browser.mcp_wrapper`, retained for older-daemon compatibility)
- `meiva/mcps/_browser.py` — in-daemon line-protocol client (the live chrome path)
- `services/desktop/browser/mcp_wrapper.py` — the legacy per-agent stdio bridge (kept as declared command; not spawned by current daemons)
- `services/desktop/browser/mcp_daemon.py` — per-machine shared upstream daemon (`browser-mcp` session)

## Notes
- Unlike installed MCPs (**installed** layer, `meiva mcp install <source>` lands in `$MEIVA_HOME/mcps/`, each with its own isolated venv), chrome is the **native/built-in** layer, implemented in the browser service and depending on a shared headed Chrome across agents — hence it spans the two subtrees mcps and services/desktop/browser.
