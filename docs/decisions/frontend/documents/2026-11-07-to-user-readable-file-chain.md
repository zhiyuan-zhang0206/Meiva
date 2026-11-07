# To user file references become readable paths

## Context

Every file the shared-context panel opened carried the SHA-256 digest of the
link's spelling in its address: `?agent_id=405&reference=4dfabec6…d788`, 64
hexadecimal characters. The 2026-11-07 user report called the URL ugly and
meaningless — it names neither the document nor the file, cannot be read,
edited or retyped, and says nothing about what a shared link opens.

The digest bought nothing a path does not. `resolve_reference` already
re-parsed each document and matched the requested value against the references
that document names; the digest was only the key it matched on, never a
capability, and every served byte was resolved from the document's own link
targets rather than from the request. What the panel needed from the key was
uniqueness inside one document, and a path has that.

## Decision

The key is the file's readable name, and the panel's `file` query parameter
carries it:

- A file inside the agent's workspace is named from the workspace root, and one
  outside it by its absolute path (`base/agents/to_user/resources.py:
  resource_path`). Only the absolute form starts with `/`, so the two cannot
  collide, and one file keeps one name however its link was spelled —
  `./plot.png`, `plot.png` and `../w/plot.png` agree.
- `ToUserResource.file` replaces `ToUserResource.id`, and `ToUserReadRequest.files`
  replaces `.references`; the HTTP parameter is `file` on both the document and
  the resource route. The chain stays a chain: each name is matched against the
  current document's own references before the next hop is read.
- A link shared before this change keeps working: the panel reads the old
  `reference` parameter when no `file` is present, and a chain element matches
  the current document's references either by readable name or by the digest
  that named it (`reference_id`). A shared link outlives the spelling of the
  address bar; the next navigation rewrites the entry with `file`.
- The address bar keeps the separators: the frontend percent-encodes a value but
  restores `/` (`ui/web/src/lib/context/reference-url.ts: encodeFile`), so a
  pushed entry reads `?agent_id=405&file=reports/runbook.md`. Form encoding would
  write `%2F` and undo the readability the change is for.

The whitelist does not move. A name is only compared to the names the traversed
documents produce, so an arbitrary path a reader supplies matches nothing and is
answered `gone`; nothing ever builds a filesystem path from the request. The
schema bounds the shape (non-empty, at most `PATH_MAX`, no NUL) so a malformed
request still fails at the gateway boundary instead of travelling to the owning
machine.

## Consequences

Readable, editable, self-describing links: a user can tell which file a URL
opens and fix an agent that pasted the wrong link.

The request schema changes, so the gateway, the ops listener and the frontend
must ship together — an old runner rejects `files` and an old frontend sends
`reference`. A digest chain still resolves, so links already shared open the
document they name, and the digest branch disappears when no link of that shape
is left to serve.

A name is stable across a rename of the *link* but not of the *file*: renaming
`plot.png` changes the name and invalidates open links to it, exactly as the
digest of a new href would have.

## Alternatives rejected

- Keep the digest and add a display name: two identities for one file, and the
  URL the user actually sees would stay the digest.
- Use the link's spelling verbatim as the key: `file:///tmp/x.pdf` and
  `../a%20b.png` are what the Markdown says, not a readable path, and two
  spellings of one file would open two different URLs.
- Flatten the chain into the recursive set of every file reachable from
  `to_user.md`: one parameter instead of one per hop, but the reader would accept
  a deep file without proving the intermediate documents still link to it, and
  the depth bound would move from "documents read" to "files enumerated".
- Percent-encode the value with form encoding: correct and invisible for the API
  fetch, but the address bar the user complained about would read
  `file=reports%2Frunbook.md`.
