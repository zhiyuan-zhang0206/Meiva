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

For a Meiva harness, the home view explains null capability (`supports_vision === null`)
with the unavailable-capabilities notice. All file and image uploads (selected, pasted
or dropped) use unified staged + batch workspace delivery across every harness and
model (`POST /api/keyed/v1/agents/{id}/uploads/staged` followed by
`POST /api/keyed/v1/agents/{id}/uploads`), delivering files to the agent's workspace
where `meiva.understand` or workspace tools inspect them. Staged attachments belong
to the selected agent; switching agents clears the composer's staged attachments
while preserving per-agent active batch tracking in `sessionStorage`. Text drafts
retain their existing per-agent persistence.
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
