# To user resources

`GET /api/agents/{id}/to-user/resource` streams one file the panel's Markdown
references, after the chain of file names (`?file=reports/runbook.md`, one per
document hop) is re-read on the owning machine. The last hop is
`resource_response`, which owns both the content disposition and the CSP a
browser applies to the bytes.

A name is only a key. The read matches it against the names the traversed
documents produce, so a path a reader invents matches nothing and is answered
`gone`, and no filesystem path is ever built from the request — the document's
own links, not the URL, decide what can be served.

## Disposition

Readability decides, never the file extension alone. Media players, the PDF
viewer and inert text render inline; archives, office documents, executables,
scripts and unknown types stay attachments so the browser never hands them to a
plugin or the desktop. Markdown itself renders in the panel, so its resource form
is the raw source a modifier-click opens.

## The HTML boundary

HTML resources download as attachments under the passive CSP (`sandbox; default-src
'none'; style-src 'unsafe-inline'`). Scripts receive no permission to execute.
The workspace viewer shows `.html` and `.htm` as source code. Reports use Markdown
with normal clickable links; only Markdown, images and videos have rendered views.
Audio, PDFs and other binary files use the workspace download view. The raw resource
endpoint continues to serve passive media, PDFs and inert text inline under the
passive CSP.

## Ongoing document

`GET /api/agents/{id}/ongoing` reads `ongoing.md` from the agent's workspace
root on the owning runner. It returns the Markdown as `content`, its
`modified_at` timestamp and a content hash as `version`. Whitespace-only
documents return null content with file metadata. Missing documents return
null for all three fields. An unknown agent returns 404. Authorized tools
can list live agents and read this endpoint for each agent to inspect current
work across the cluster.

## Workspace file reads

`GET /api/agents/{id}/files/content` and `GET /api/agents/{id}/files/raw` serve
interactive file previews and raw media, either directly on the gateway host or
through `POST /to-user/workspace-file` on the owning runner. After checking
caller evaluation isolation, an explicit `.meiva/workspaces/<owner>/<subpath>`
(or `<home>/workspaces/<owner>/<subpath>`) reference switches placement and file
resolution to `<owner>`'s workspace (`custom_cwd=None`), resolving cross-agent
references across machines and host usernames; any other path is read on the
agent's own machine. The working directory comes from the latest root-namespace
checkpoint (`checkpoint_ns = ''`), never a subgraph's working directory. The
namespace predicate also keeps this lookup on the checkpoint primary key's
ordered, single-row read as an agent's history grows.

How far a read reaches is the cluster setting `settings.to_user.file_access`
([to-user limits](../../../base/config/docs/to-user-limits.md)). The gateway
decides and sends it with every forwarded read, so the runner applies the
gateway's setting rather than its own:

- `all` (default): any path the account can read on the owning machine,
  absolute, `~/...` (that machine's account home) or relative to the agent's
  working directory or workspace. `..` in an absolute path is resolved by its
  text first, so a path that leaves one workspace is routed by where it lands.
- `workspaces`: the agent's workspaces and working directory only. Subpaths that
  traverse above the workspace root with `..`, and absolute paths outside the
  target agent's allowed roots, are refused with `403`.

In both, only a regular file is served (a directory, socket, FIFO or device is
`404`), an unreadable one is `403`, and the size limits hold. A content answer
carries the file's absolute resolved `path` and its `root`: where the viewer's
path bar starts on the owning machine. That is `{kind: "workspace", path, agent_id}`
when the file is inside an agent workspace, `{kind: "home", path, agent_id: null}`
under the account home, and `{kind: "machine", path: "/", agent_id: null}`
otherwise.

`/files/raw` answers HTTP byte ranges on both paths, so media elements can seek
and Safari can play at all. On the gateway host `FileResponse` serves them; a
forwarded read carries the browser's `Range` to the runner as `byte_range`, the
runner answers that slice (`206`, or `416` past the end, with its
`Content-Range`, at most 8 MiB per answer, which the media element follows with
its next range) and the gateway streams the answer through instead of buffering
it. A request without a range, or with one HTTP lets a server ignore (several
ranges, another unit), gets the whole file under `max_resource_bytes`; so does a
raw read whose runner predates `byte_range` or `file_access` and refuses it (it is
asked again as that runner knows it, within workspace reach).

## Workspace browsing

`GET /api/agents/{id}/files/list?path=<dir>` lists one directory for the panel's
file viewer, under the same reach, refusals and forwarding as a file read
(`mode="list"` on `/to-user/workspace-file`). Without `path` it lists the
agent's workspace root; in `all` reach `~` and `/` list the account home and the
filesystem root. The answer carries the directory's absolute `path`, its `root`
(as above) and `parent`, which is null at a root the viewer may not climb out of:
`/` in `all` reach, an agent workspace or working directory in `workspaces`
reach. Directories come first, then files, each by name; sockets, FIFOs and
dangling links are not listed, and in `workspaces` reach neither is a symlink
whose target leaves the allowed roots. At most 1,000 entries come back
(`truncated` says more exist), because the tree renders a row per entry and the
forwarded answer is one JSON body.

## Durable user communication

`to_user.md` is an editable overview. Unread display/publication messages and
required replies are separate durable records, shown alongside it in the shared
context panel. Replacing the overview cannot replace those records. Reading a
message clears only unread state; answering or explicitly closing resolves a
required reply. See [meiva.ui — User Interface](../../../meiva/docs/ui.md).
