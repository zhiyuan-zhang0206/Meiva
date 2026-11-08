# meiva:note
# One entry per model SDK: a single root import that no other module may shadow with a submodule
# import of its own. `anthropic_sdk.py` states the concurrent-first-import race all three prevent.
# This package imports nothing itself, so a process that reaches no model SDK pays nothing here.
# meiva:link base/lm/compat/docs/compat.meiva.okf.md
