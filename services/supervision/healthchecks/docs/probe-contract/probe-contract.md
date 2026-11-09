# Probe evidence contract

A service is ALIVE when its configured application protocol succeeds. Frontend
probes the application behind the entry gate; collector readiness sends a valid
OTLP request; Unix services answer a bounded JSON ping. Read-only observations
do not inspect native listener ancestry or compare process generations.

A protocol failure is DOWN. Missing configuration or an exception in a custom
probe yields UNAVAILABLE through root's `ProbeRunner`. A successful response is
availability evidence, not authority to adopt or signal the responding process.
Stop and restart retain their lifecycle owner's scope.

Each runner retains at most one outstanding worker. A timed-out native call
cannot be canceled by Python, so subsequent rounds report it as unavailable
without launching more copies. A late result is discarded; the next result must
come from a fresh observation. These limits apply to service and diagnostic
probes. Root recovery remains a separate consumer of evidence.

See [browser evidence](browser-two-questions.md),
[verdict policy](../terminal-verdict/terminal-verdict.md),
and [native data-plane diagnostics](../../../meiva_root_glue/docs/diagnostics.md).
