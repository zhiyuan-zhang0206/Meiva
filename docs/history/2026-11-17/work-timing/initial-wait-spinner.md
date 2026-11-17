# Initial conversation waits use a single spinner

The user's 2026-11-17 ruling selected spinner-only feedback while entering or
switching conversations and while waiting for the first actual work step.
The previous pending row used a `Working` title and disclosure chevron before
it contained expandable work, and could appear alongside the initial-load ring.

Initial source qualification was chosen as the presentation owner, including
when retained rows exist. Reader paging follows it in priority, and the inline
first-step wait follows reader paging. This preserves one waiting indicator
without treating retained row count as proof of source readiness.

Real work blocks retained their content, expansion and lifecycle status. No
timeout was introduced to end a run. Current rendering behavior lives in
[the loading ring contract](../../../../ui/web/src/docs/frontend-components/timeline-view/loading-ring.md).
