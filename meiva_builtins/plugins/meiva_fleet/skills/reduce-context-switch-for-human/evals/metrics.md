# Attention instruction evaluation

Baseline: GitHub main `4607267c39bdf52cd6f2314d2903f8bf2c287d8d`.
Candidate: the exact PR head recorded in its handoff. Changed variable: shared
communication instructions and Fleet scope, with delivery machinery unchanged.

These constructed cases need no credentials or side effects. For instruction
review, compare baseline and candidate text against each expected route and
visibility. Record supported, ambiguous or contradicted, with source evidence.
Acceptance: every case supported without a conflicting completion/routing rule;
important answers, results, corrections and changed promises must remain visible.
Suppression of more messages or shorter messages is not a quality metric.

Prompt assembly tests check that all delivery modes and native/external callers
retain the same attention policy. They do not measure agent decisions. Record
case-level reviews, source identities and test results outside the skill package.
If a model execution comparison is authorized later, use the same cases, model,
effort and fixtures in fresh isolated contexts for both versions, capture actual
outputs/costs, and grade routing, retained useful content and truthful reporting.
Do not report manual instruction review as model pass rates or production behavior.
