# The file viewer reaches the whole machine by default

## Context

The right-hand panel became a file viewer with a tree. Its reads
(`/files/content`, `/files/raw`, `/files/list`) inherited the boundary built for
opening links from a report: agent workspaces plus the agent's working
directory. On 2026-11-21 (01:32) the user ruled that the viewer should show any
file on the machine by default, with the narrower boundary kept as a setting.

## Decision

- `settings.to_user.file_access`: `all` (default) or `workspaces`. Cluster-pinned
  like the other `to_user` read settings.
- The gateway decides. It sends its setting with every forwarded read
  (`WorkspaceFileRequest.file_access`), and the owning runner applies the value
  it receives. A request without the field means `workspaces`, so a gateway that
  predates the setting cannot widen a runner.
- Reach widens *where* a read may go, not *what* it serves: only regular files,
  the account's own permissions, and the existing size limits, in both modes.
- Every content and listing answer names its path-bar root (`workspace` with the
  agent id, `home`, or `machine`) and carries the absolute resolved path. The UI
  draws `#63 › a`, `~ › x` or `/ › etc` without guessing the owning machine's
  layout.

## Alternatives rejected

- **The runner decides from its own config.** Two copies of one decision would
  disagree during a rolling update or after a local override. The gateway is the
  one place a user's request passes through.
- **A per-agent setting.** The reach belongs to the human viewing the machine,
  not to the agent whose conversation the panel happens to show.

## Consequences

- Anyone signed in to the gateway can read any file the gateway's and runners'
  accounts can read, including credentials under the home directory.
  `workspaces` is the switch for a cluster that should not allow that.
- During a rolling update, content and listing reads forwarded to a runner that
  predates the field fail until it updates. Raw reads are asked again without
  the newer fields, and are served within workspace reach.
