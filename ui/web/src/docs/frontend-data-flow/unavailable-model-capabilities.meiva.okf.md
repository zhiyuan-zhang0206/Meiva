---
type: doc
title: Unavailable model capabilities
description: "Frontend behavior when model capability metadata is unavailable: nullable supports_vision, gating, accepted-receipt limits, and browser-test scope."
tags: [frontend]
---

# Unavailable model capabilities

The required nullable `supports_vision` field distinguishes unavailable model
metadata from a known text-only model. A successful roster or selected-detail
read replaces the previous capability value, including true-to-null changes;
lifecycle SSE events remain invalidation hints and cannot resurrect old metadata.
Agent status, history selection and bounded directory pagination retain their
existing meanings. See [[base/agents/observation/docs/observation.meiva.okf.md]].

For a Meiva harness, the composer explains null capability and enables native
image attachment only for true. External harness adapters currently accept text,
so a configured history model with vision never enables native image attachment
for them. Pasted and dropped images use ordinary file delivery instead, with a
workspace file receipt rather than a model-image thumbnail. File delivery remains
a separate upload/receipt operation. Native image attachments belong to the
selected agent and the current native-image capability. Changing either clears
pending and ready attachments before another send and releases their local
previews, including uploads started before the thumbnail first renders. A completed upload updates only an attachment still in that scope; an
old completion cannot restore a removed image after switching back. Text drafts
retain their existing per-agent persistence and ordinary file delivery is
unchanged. Replacing the attachment callback alone does not discard images.
Text sending and exact-work stop remain accessible; their accepted receipts do
not claim model execution or work completion. A failed models query hides cached
model choices and disables spawning, with an unavailable label in both expanded
and collapsed controls. Ordinary Control configuration remains editable.

The isolated browser test `tests/e2e/flow/test_provider_failure.py` starts the
real gateway with a provider module that raises, reads retained checkpoint
history, posts text and requests a stop through the real page. It verifies a
pending inbound and accepted cancellation while the recorded work remains
running; it does not run a model or assert a worker exited.

External harnesses also hide the composer and inspector context meters and the
sidebar Compact action. Those controls describe or rewrite the Meiva history
mirror; they neither measure nor compact the external CLI session. Restart,
terminate, text input and ordinary file delivery remain available.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md|Frontend Data Flow]] — the node this availability behavior belongs to; the field's owner is [[base/agents/observation/docs/observation.meiva.okf.md|observation]].
