---
type: doc
title: Model Presentation
description: Harness billing, native API model visibility, catalog grouping, rates, and defaults in creation and display settings.
tags:
- frontend
---

# Model Presentation

Model IDs remain the exact stored and request values. A shared presentation
helper renders human-readable names in both spawn layouts, Control model lists
and the inspector: `gemini-4-argon` becomes `Gemini 4 Argon`,
`claude-fable-5-1` becomes `Claude Fable 5.1`, and `gpt-6.1-sol` becomes
`GPT 6.1 Sol`. Effort is shown as `High`, `Extra High`, or the other readable
level names; the request still sends `high`, `xhigh`, and the original values.
The model catalog is local registry metadata and its GET does not probe CLIs.

Rates appear only for a **metered** harness. Billing travels with the harness
(`HarnessAvailability.billing`: `api` for the built-in runtime, `subscription`
for the external CLIs), because the same model costs differently depending on
which loop runs it: a subscription pays nothing per token, so a per-million-token
price beside the model name would describe a cost the user is not incurring. For
a metered harness the model row carries the three numbers that decide it —
non-cached input, cached input (cache read), output, in USD per million tokens —
on one smaller line under the model name, so the name keeps the column's width
(user 2026-10-21); the declared 5-minute and 1-hour cache-write rates and the
vendor reference TPS (with its source and check date) are on hover. Standard and Fast service IDs have separate catalog entries and
prices. Absent reliable absolute vendor figures say so instead of
showing a blank column; relative speed multipliers and cluster measurements
never stand in for the vendor figure.

Declared `fast_of` services appear directly after their visible Standard model.
Families sort by the Standard input price; a service whose Standard model is
hidden remains an independent row sorted by its own price. Names do not imply
family membership; grouping preserves the chosen harness's visibility filters.

Control > Display draws the same catalog two levels deep (`_model_picker.tsx`): the
provider is an uppercase heading on a tinted band carrying its master switch, its models
start at the band's own text edge under a lighter separator, and each family is a
`role=group` named by that heading. A provider is named once per family; a model row states itself
and its prices. Switching a provider off hides its native API models and leaves
the per-model switches non-editable; each model keeps its state.

The published default uses the same database/config resolution as a new
agent's birth stamp. Saving the cluster default invalidates both default-model
and model-catalog queries, including the otherwise indefinitely cached picker.

Rates below USD 1 use up to four significant digits, with at least two decimal
places, so nonzero cache prices such as USD 0.003 never display as USD 0.00.
The price tooltip labels input/cache-read/output, explains that the displayed
rates cover the current base tier, and includes declared 5-minute/1-hour
cache-write rates. Longer contexts and recurring rate windows can select
different rates for actual billing.
