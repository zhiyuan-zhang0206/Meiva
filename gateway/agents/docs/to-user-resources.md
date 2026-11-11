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

## Durable user communication

`to_user.md` is an editable overview. Unread display/publication messages and
required replies are separate durable records, shown alongside it in the shared
context panel. Replacing the overview cannot replace those records. Reading a
message clears only unread state; answering or explicitly closing resolves a
required reply. See [meiva.ui — User Interface](../../../meiva/docs/ui.md).
