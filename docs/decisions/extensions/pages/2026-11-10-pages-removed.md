# Pages are removed

## Context

Pages let an agent start an HTML page (`meiva.ui.serve` / `meiva.ui.show`) that
the gateway proxied under `/pages/<id>-<name>/` and whose callbacks reached the
agent as `ui:page:<name>` messages. A supervised `page-server` daemon owned the
serve() page processes. User delivery moved to `meiva.ui.publish` / `meiva.ui.ask`
and the shared context panel (`to_user.md`), which do not need an agent-owned web
server.

## Decision

The subsystem is deleted in two steps. The first removed the SDK entries, the
gateway page routes and proxy, the `agent_pages` / `page_operation_receipts`
tables, the frontend page links and the `meiva-guide/pages` resources. The second
removed what that left behind: the `page-server` daemon and its roster entry,
health port and settings, the `PageOpened` / `PageClosed` live events, the
page-related log events, the `page-` shell label reservation (shell names no
longer have a reserved label), the terminate paths' page bookkeeping, and the
`ui:page:<name>` message source. `user` is the only human source.

## Alternatives rejected

- **Keep `ui:page:` as an accepted source.** Nothing can send it any more, and a
  source with no writer makes every human-source check carry a dead branch.
- **Leave the daemon running idle.** It queried a table that no longer exists.

## Consequences

Earlier entries in this directory, and the page entries elsewhere under
`docs/decisions/`, describe a mechanism that no longer exists and are kept as
history. Stored messages with a `ui:page:` source are not rewritten here.

<!-- Supersedes: 2026-08-23-page-server-missing-serve-dir.md, 2026-10-02-dead-show-pages-are-scanned-by-the-page-server.md, 2026-10-06-page-waiting-knowledge-owners.md (the page parts), ../../agents/lifecycle/2026-08-25-terminate-keeps-daemon-pages.md, ../../runtime/processes/sessions/2026-08-24-page-server-agent-shell-sessions.md -->
