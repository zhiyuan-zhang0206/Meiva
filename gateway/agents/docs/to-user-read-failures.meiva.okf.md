---
type: doc
title: To user read failures
description: Why a panel read is refused, and the one class name the ops listener, the gateway and the panel each speak.
tags: [gateway, to-user]
---

# To user read failures

A panel read is served by the agent's home machine, so a refusal happens three
steps away from the browser. `base/agents/to_user/errors.py` owns the classes;
`read_failure` maps every `OSError`/`ValueError` a read can raise onto one of
them. `ops/to_user.read_failure_payload` carries the class as `reason` in the
failed op result, next to the local `error` text the gateway logs.

| Class | Raised by | Answer |
|---|---|---|
| `gone` | the id is no longer in the document, or the file is missing | `404 to_user_reference_gone` |
| `too_large` | `read_regular_file` past the route's byte budget | `413 to_user_document_too_large` |
| `unreadable` | not a regular file, not UTF-8, a symlinked hop, permission | `422 to_user_document_unreadable` |

`gateway/agents/to_user/document.py` owns the response: it maps the class to
the status and the envelope `code` above and states `retryable: false` for all
three. A read that reaches the machine fails differently: `ClusterOpUnreachable`
stays `503`, the one answer that says the same request may pass. A failed op
result without a known reason is logged as a warning and answered as
`unreadable`, so an unexpected cause is still visible in the log.

## Why the class, not the status

Every refusal used to be `422` with one detail (`The workspace document could
not be read`), so a missing file, a stale id, a file past the budget and an
undecodable file were the same sentence for the reader — and the panel told them
all to try again, which can only re-run the same read. The class is what the
panel renders: `to_user_document_too_large` offers the file itself through the
resource route (which serves it under `max_resource_bytes`), `to_user_reference_gone`
says the link is gone, and only a retryable answer asks for a retry. The panel's
copy is chosen from `code`, never from the status
([shared context](../../../ui/web/src/docs/frontend-state/shared-context.meiva.okf.md)).

## What stays refused

`settings.to_user.max_document_bytes` (1 MiB) still bounds every document the
panel renders, including each hop of a reference chain. Measured on
2026-10-28, a 282 KB status panel reads in 0.26 s and a 1.2 MB report in 0.79 s
with the budget raised, but a link-dense document costs about 2.6 s per MiB
(11 000 links per MiB), and this gateway read carries a three-second cluster
budget — so the real ceiling depends on the document's link density, not on its
size, and the browser's render of the Markdown comes on top. A document past
the budget is reachable through the resource route, which streams bounded bytes
without parsing them.
