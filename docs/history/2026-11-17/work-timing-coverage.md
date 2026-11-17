# Work timing coverage

At main `8476464a2ff8feee4cea1e26b2dc009da3083a54`, the work header sums per-item measurements, replacing missing durations with zero. This can present a tiny execution subtotal as the complete work duration. The reported screenshot showed 12 turns and 0.1 seconds.

Choose an explicit incomplete/unrecorded label while preserving the measured sum and real zero values. Do not reconstruct active work from adjacent timeline timestamps: restart and wake idle gaps would enter the sum. The frontend clock remains provisional; a canonical record without duration must expose that loss after reload.

A bounded 65-item agent 405 timeline sample had 17 reasoning, 21 code and 21 output records with no durations. The last 55 Antigravity transcript rows included 26 planner and 23 generic rows with no duration fields. In contrast, a live GetCascadeTrajectory read returned plannerResponse.thinkingDuration and metadata.internalMetadata.statusTransitions timestamps. The current trajectory decoder retains type/clientMessageId only, and the transcript parser has no timing enrichment. This change corrects presentation; it does not restore collection. A separate collector needs step identity reconciliation, protobuf duration parsing, terminal status interpretation and overlap accounting before those values can become authoritative totals.

Update: the user selected a plain "Worked" header with a visible warning for incomplete timing ("Working…" while active). The tooltip and accessible name explain the missing data; the partial subtotal remains internal. This supersedes the earlier partial-subtotal title proposal.

Update: [Timing diagnostics belong in cluster events](timing-cluster-diagnostics.md) supersedes the visible-warning decision after the user's 20:59:50 ruling.
