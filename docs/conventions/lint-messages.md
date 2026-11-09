# The four things a lint message carries

A failing check speaks to whoever hit it, at the moment they have the most
context and the least patience. The message therefore has to teach the rule, not
only name the violation: an author who has to go look up what a code meant will
fix the line and leave the cause. Every failing message — error or warning —
carries four parts, in this order.

1. **The fact.** What was measured against what was expected, and where: the
   file, the line or the entry, in the form the tool already prints
   (`path:line: E007: …`). These fields stay machine-readable and unchanged from
   what the check compared, so a reader can find the object without reading the
   prose.
2. **Why the rule exists.** The property it protects in the repository over
   time — what stays true when the rule is obeyed. "One node holds one semantic
   topic" states a design fact; "the linter requires it" states nothing, and an
   author cannot reason about it.
3. **Who owns it now.** The author who hit the check owns the *semantic* repair:
   the structural change the rule is asking for, in the terms of the object in
   front of them. Where passing by defeating the check is a real temptation
   (trimming prose, deleting the test, widening an exemption), the message names
   that move as the thing not to do.
4. **Where the rule is written.** A pointer to the convention, decision record or
   example node that owns the rule, so the author can read the whole thing
   instead of reverse-engineering it from one message.

## One hard line, no headroom

A gate has a single hard line, with no warn band inside it and no report of the
room left. A "23 characters left of 8000" readout turns the line into a budget to
fill and moves the pressure from *structure* to *the last few characters*: authors
spend it on compression that deletes facts instead of on the split the rule asks
for. A check that has more to say says it in part 2, not in a second threshold.
Crossing the line is paid once, by the change that crosses it, and that change's
author does the work.

### Report layering is not a soft limit

Not every second number is a warning band. A quality metric can report a warn
tier beside its hard failure tier, and that is a *reporting layer*: it grants
nothing and permits nothing, and no range of values becomes acceptable by sitting
under the warn line. A soft limit is the opposite — it says a range *below* the
line is already wrong, which is what turns the line into a budget to fill. The
test is whether the warning admits a permitted interval; no gate here reports
one (the complexity tiers were reduced to their hard budget in `07c09b31b`).

The generic `soft` threshold in `base/packages/text_budget.py` stays as it is: it
has non-gate callers (the memory index's own cap reminder). Keeping a capability
is not permission to add a band to a contribution gate.

## Shape of the text

- One or two sentences per part. It is a message, not an essay, and the numbers
  come first.
- Wording lives in named constants at the check's own site, next to the values it
  describes, rather than inline in the emitting code — so the four parts stay
  visible as parts and a second check can reuse the shape.
- Tests assert the parts are present (key phrases: the measured value, the
  reason's core phrase, the remedy) and that the boundaries still fire. They do
  not pin the sentences, so wording can improve without a test rewrite.

## Worked example

The `AGENTS.md` size ceiling in
[`scripts/lint/agents_md_size.py`](../../scripts/lint/agents_md_size.py)
contributes all four:

```
AGENTS.md: 312 lines (max 300).
AGENTS.md is loaded into every agent's context; lines spent here cost every turn
whether the task needs them or not: this line marks a root file that outgrew its
role as an index — it is not a budget to fill. Move detailed rules into the
relevant document under docs/conventions/ and leave a one-line link here; do not
trim or compress rules to fit. See docs/conventions/doc-maintenance.md.
```

Part 1 is `312 lines (max 300)` plus the printed location;
part 2 is the context-budget reason; part 3 is the move into `docs/conventions/`
and the trim it forbids; part 4 is the reference to
[documentation maintenance](doc-maintenance.md).

## Inventory

The checks that still owe one of the four parts are inventoried in the discussion
of the change that introduced this convention (#7742) rather than in the tree:
that list is a dated snapshot with owners and suggested fixes, and a rule is
better served by the one paragraph it needs here.
