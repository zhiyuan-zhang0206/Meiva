# Wiring the Claude Agent SDK session into the turn model

This note is the **predecessor** of
[external harness adapters](2026-10-19-external-harness.md). Its design content
now lives there — §13 (wiring the adapter into the turn model) and §14
(consequences) — because the same design has to hold for Codex and Antigravity,
not only for Claude.

It is kept for history: the original scope ruling (Python code mode, no MCP
server, no system-prompt rewrite, no bridge-level permission layer, a stable
agent id bound to the external session so `resurrect` works) was made against a
Claude-only bridge. The package it describes is now `base/lm/harness/claude/`,
the Claude adapter under the shared `base/lm/harness/`.
