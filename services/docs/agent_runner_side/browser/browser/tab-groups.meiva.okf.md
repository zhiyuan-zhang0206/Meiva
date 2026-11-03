---
type: doc
title: Browser Tab Groups
description: Every page an agent's browser call creates is filed into one Chrome tab group per agent across the browser, through a staged unpacked extension the browser-mcp daemon loads over CDP.
tags:
- browser
---

# Browser Tab Groups

The shared Chrome shows at most one open tab group per agent across the browser, titled `Meiva #<id>` (ids are unique, labels are not). It holds every page that agent's browser calls **created**: an explicit `new_page` and the page a page-less first `navigate_page` auto-creates. A page the agent merely selected (a user's tab) is never grouped, nor is a page opened in an isolated browser context (`isolatedContext`): extensions cannot see such tabs. The title carries no TTL and no color; Chrome assigns the color.

The owning document is [the component doc](../../../../desktop/browser/docs/tab-groups.meiva.okf.md); this page is the services-index view of it.

## Mechanism

- **No CDP API.** Chrome's DevTools protocol has no tab-group commands, so `services/desktop/browser/tab_groups/extension/` ships a small MV3 extension (permissions `tabs`, `tabGroups`, `debugger`, plus a fixed manifest `key`). Its service worker exposes `meivaGroup(targetId, title)`: map the CDP target to a tab (`chrome.debugger.getTargets()`, no attach, no debugger bar), converge all open groups with the agent's title across the browser into one keeper (most tabs, ties to the lowest group id, anchored in the keeper's window via `chrome.tabGroups.move`), else create the group, set the title, and re-converge. It keeps no state, so daemon restarts and Chrome restarts need no mapping repair.
- **Single staged path and stale-copy cleanup.** `ExtensionWorker` syncs `manifest.json` and the hashed worker script atomically into `<home>/run/tab-groups-extension`, uninstalls any older `"Meiva Tab Groups"` copies loaded from release/worktree paths via `Extensions.uninstall`, and loads only the staged path. The fixed manifest `key` keeps one permanent extension id so toolbar unpin state persists across updates and reloads.
- **Loaded when needed under one lock.** `Extensions.loadUnpacked` works on the ordinary `--remote-debugging-port` connection (no launch flag), the load lasts only for that Chrome session, and the worker stops about 30 seconds after its last event (or stays listed on Chrome 156 while refusing `Target.attachToTarget`). `ExtensionWorker` checks for an attachable worker of the shipped file name on each call and reloads only when absent or dead, serializing `session_restore.reapply`, `converge_all`, `group_new_page` and `release_page` under one lock so calls never reload the worker underneath each other.
- **The worker file name carries a content hash** (`group-<sha256[:12]>.js`, enforced by a test). Chrome caches an extension's worker script in the profile and keeps running the cached copy when only the content or the manifest version changes; a new file name is the only reliable update. Edit the worker, rename it, update `manifest.json`.
- **Page id to target id.** The daemon's page ids are chrome-devtools-mcp sequence numbers, not CDP targets. `ChromeMcpDaemon._new_page` runs under the machine-wide serial lock: `AgentTabGroups.snapshot()` lists Chrome's page targets (`/json/list`) before the upstream `new_page`, and `group_new_page()` lists them again after and takes the one new target. Any other count (a user opened a tab meanwhile, a popup) is reported and left ungrouped; it never guesses. Those two listings (2 s timeout each) are all that runs under the daemon lock.
- **Grouping runs off the lock.** Loading the extension, attaching and calling `meivaGroup` happen in a background task (10 s timeout) started by `group_new_page()`, so a slow or wedged Chrome never delays page creation or another agent's call. `cancel_pending()` drops them when the daemon stops.
- **The extension ships in the package.** Hatch packages the `services` tree whole, so the unpacked extension source directory is installed beside the code and staged into `<home>/run/tab-groups-extension` at runtime.

## Removal is Chrome's, not ours

Nothing deletes a group, and a closed group is **not** removed: Chrome keeps a group whose last tab closes as a *saved* group on the bookmarks bar, outside the extension API (`chrome.tabGroups.query` sees only open groups, `query({saved: true})` is rejected, and the namespace has no delete). The stack therefore ungroups an agent's last tab **before** closing it (`AgentTabGroups.release_page` -> `meivaUngroupLast`), waking an idled-out service worker when needed so closing a tab >30s after creation deletes the group instead of saving it. Every path that closes an agent's pages goes through the upstream `close_page`, so that is the one hook needed; a tab the user closes by hand still leaves a chip nothing here can remove. See [the owning document](../../../../desktop/browser/docs/tab-groups.meiva.okf.md) for the measured details.

## Failure policy

Grouping is presentation only. A failed load, CDP error, missing worker, ambiguous target or wedged Chrome logs a WARNING (with the cause) and the page operation proceeds untouched: `new_page` and `navigate_page` never fail because of grouping. This is a bounded exception to fail-fast; see [the decision](../../../../../docs/decisions/engineering/design/contracts/2026-10-12-presentation-only-failure-degrades-with-warning.md).

Parent: [[services/docs/agent_runner_side/browser/browser/browser.meiva.okf.md]]
