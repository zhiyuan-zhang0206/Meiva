# Keep system markers inside Work Blocks

The user requested that internal messages, system notices, errors, retries and command results remain in Work Blocks, with human-facing replies in the conversation body.

Agent 405's timeline item `h18417887.0` carried `kind=system_marker`, `source=background_task`, and `user_facing=true`. Its status payload contained the task-6775 completion, command exit code, output and log URL. The transcript already supplied structured provenance; no text inference or Notice projection change was needed. The former grouping rule treated markers outside the NoteTag card families as bare rows, splitting adjacent work.

All rendered system markers now use secondary grouping. Existing silent bookkeeping filters and raw error/status/unknown-source renderers remain in place. A standalone error gets its own expandable Work Block; explicit user-facing agent replies retain their primary placement. Unknown item-kind handling and work-duration accounting are unchanged.

Validation exercises live updates, the SSE error reducer, settling and serialized reload, single and adjacent markers, silent bookkeeping and human-facing replies. The grouping regression failed against the previous rule before the implementation changed.
