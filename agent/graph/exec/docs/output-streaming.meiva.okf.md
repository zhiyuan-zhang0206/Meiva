---
type: doc
title: Exec Output Streaming and Accumulation Cap
description: How the exec parent drains the child's merged output into a streaming accumulator, pushes chunks to Redis in real time, keeps a silent exec visible with SSE-only keepalives, and bounds a runaway print loop with a first-half/last-half accumulation cap instead of killing the run.
tags: []
---

# Exec Output Streaming and Accumulation Cap

## Responsibility

Between the child's pipe and the `ToolMessage` the model reads, the parent owns
one accumulator. This node records how output is drained, streamed and bounded
while the code runs; the child lifecycle, timeouts and cleanup are in
[[agent/graph/exec/docs/exec.meiva.okf.md|Tool Execution]], and the final
envelope and previews are [[agent/graph/exec/docs/output-preview.meiva.okf.md]]'s.

## Streaming

- The child writes stdout/stderr line-buffered onto the pipe (chronological merge); the parent drains the pipe into a `StreamingTextIO` (`_stream.py`) and streams chunks to Redis in real time. While a live child is silent, the same parent poll loop emits an empty SSE-only keepalive after each 0.5s without output (the beat plan's `keepalive`; the loop holds the last-activity instant) so a frontend that joined mid-exec can materialize the running output block; keepalives never enter the accumulator, child envelopes, or LangGraph messages. The parent finally returns the unchanged accumulator envelope to the LLM

## Accumulation cap

- **Accumulation cap** (`exec_output_accumulation_max_chars`, default 1,000,000): the accumulator pins its first half and rolls its last half, dropping the middle **as the code runs**, so a runaway `print` loop cannot grow the child until it is OOM-killed. Execution is **not** killed — the output is truncated with an explicit marker and the model self-corrects; the result taxonomy is unchanged. Redis pushes come off the same accumulator so they inherit the bound: past the budget the retained text is a rolling window with no append-only increment, so one notice goes out and streaming stops until the final `ExecOutput` upsert

## Entry Points

- `agent/graph/exec/_stream.py` — `StreamingTextIO` + `ExecOutputChunkPublisher` (streaming output incremental push)
- `agent/graph/exec/_poll.py` — the poll-beat plan that decides forwarding, keepalive and stop

Parent: [[agent/graph/exec/docs/exec.meiva.okf.md|Tool Execution]].
