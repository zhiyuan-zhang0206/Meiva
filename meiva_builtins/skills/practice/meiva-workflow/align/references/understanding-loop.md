# Understanding Loop

A way to study one problem with a human until they can predict, explain and
judge it themselves. Evidence arrives a piece at a time, the model is revised
between pieces, and the loop ends when the question that started it is
answerable. This guide covers that destination; Align owns the calibration.

## When it applies

The human wants to be able to judge the subject afterwards, not only to receive
a result from it. A live session changes the pacing and nothing else: the same
loop runs asynchronously, with pieces of evidence and open questions waiting in
notes until the human returns (see [With Immersive](#with-immersive)).

Not this guide:

- **The result is the whole point.** Getting a CI job green, renaming an API:
  the human wants the check to pass, not the failure's causal chain.
- **An operation with nothing to understand.** Pressing the buttons a runbook
  names, waiting for a deployment to finish: the human needs the current state
  and the next step.

## The depth target

What the human should end up able to do with the subject:

- **Predict** a result that has not happened yet: which of two cases will fail,
  what a change will cost, which objection a review will raise.
- **Explain** the causal chain: why this outcome follows from that mechanism,
  and which step would break it.
- **Judge** a trade-off: which option to take, and what would make that choice
  wrong.

The target is a direction, not an exam. None of these is separately required,
and no piece of evidence is graded against them: they are a ruler for how far
the shared model still is from the subject, and the reading decides whether
another piece is worth its cost. The loop ends when the starting question can
be answered — what would happen, why, and what would change the choice — and
whatever stays unsettled is named as a gap, not counted against anyone.

## How the work moves

The loop alternates evidence and reasoning. These moves are available rather
than a script every round must run, and who leads changes with the question.

- **Bring one piece.** One artifact, contrast, counterexample, or trace — the
  smallest piece that carries a decision. Searches, reads and replays happen
  behind it; what reaches the human stays small enough to inspect.
- **Ask when an answer would change something.** A question about how the human
  reads this piece, or what they expect next. The human may instead lead with
  their own question or explanation and the agent answers that; a question that
  cannot move understanding is not worth the attention it costs.
- **Offer a model the human can test.** The agent states its own account
  independently — what it expects, and why — before comparing. Where the two
  models differ, the difference locates the belief worth checking, and the
  agent's model can lead when a piece would otherwise hang without one.
- **Explain at the level asked for.** The mechanism, the evidence it rests on,
  and the parts still inferred, kept separate.
- **Judge.** A decision or a prediction, with the reasons behind it. Those
  reasons choose the next piece, and a correction revises the agent's model.

Where the next step would rest on an untested belief, a prediction that can
fail — a specific outcome, a case, a number — turns the next piece into a test
of it. The agent proposes the pieces where a prediction would settle something,
and the human and agent choose among them together. Not every piece needs one,
and a prediction nothing could contradict teaches nothing.

While the conversation moves or waits on the human, the agent works the same
question in the background instead of filling the pause — preparing the next
piece, testing the model on an adjacent case, or running a silent read-only
tracer probe on an untested factual assumption. A background probe enters the
dialogue only when it contradicts a working belief, resolves a split between
models, or yields a concrete new piece; uneventful checks stay silent.

Replaying a past case keeps the same shape, with the record standing in for the
live question. What is observed is how the human's model, explanation and
judgment were revised: which piece moved them, and toward what — a prediction
being one optional kind of evidence, not the measure of understanding.

## With Immersive

In a live session the `meiva-immersive-mode` skill
(`meiva.help(meiva.skills.meiva_immersive_mode)`) owns the cadence: the surface
rhythm and silence watchdog cap, the ledger, and what reverts at the end.
This guide owns what each piece of evidence feeds. Without a session the guide
is unchanged — pieces and questions wait in notes for the human to return to —
and only the cadence contract drops away.

The two are independent. A live operation may need only the current state and
the next step, with no loop at all; a loop can run asynchronously for days
without a session contract.

Replayed once against an existing record, not yet used live on a fresh
question: a live use should record which piece of evidence moved the human's
model, explanation or judgment, and in which direction.
