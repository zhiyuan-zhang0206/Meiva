---
name: inspect-a-trace
description: "Runs the trace fetch, read and render scripts from a Meiva source checkout. Use when troubleshooting has given a trace id and you want the whole trace as a span tree or an offline report."
---

# Inspect a trace (scripts)

The method for finding out what happened to an agent (events, Loki, Tempo, metrics, then local
files) lives in the guide's
[troubleshooting](../../../meiva_builtins/skills/platform/meiva-guide/troubleshooting/SKILL.md)
skill, and works from any machine. This directory owns only the toolchain scripts that need a
source checkout and its `.venv`:

```
scripts/fetch_trace.py ─▶ trace_raw.json ─▶ read_trace.py ─▶ trace_read.json ─▶ render_trace.py ─▶ report/
```

They encode the conversions you would otherwise re-derive (base64 vs hex ids, OTLP envelopes,
the 5 MB full-trace cap, gateway bearer auth). Usage, flags and Tempo pitfalls:
[spans](../../../meiva_builtins/skills/platform/meiva-guide/troubleshooting/references/spans.md).
Run them from the checkout root as `.venv/bin/python .agents/skills/inspect-a-trace/scripts/<script>`.
