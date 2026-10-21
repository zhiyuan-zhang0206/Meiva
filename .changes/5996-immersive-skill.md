type: Added

- A new built-in `meiva-immersive` skill keeps a live human conversation fed while
  heavy work runs in parallel: the human turns it on ("immersive on"), the agent
  announces the silence cap (default 60s), the inline/delegate split and how the
  session ends, keeps a durable ledger in its workspace, and reverts to the default
  batched reporting on exit. It never enters on its own — an agent may offer a live
  pass once, because where the human's attention goes is the human's decision —
  ([immersive skill](meiva_builtins/skills/coordination/meiva-immersive/docs/meiva-immersive.meiva.okf.md)).
