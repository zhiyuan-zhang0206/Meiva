# Memory documentation contracts

The memory references now follow the runtime split and Two-Stage OKF discipline.
Runtime registration belongs to agent_runtime.py; plugin.py owns the SDK surface.
The prompt now calls for inline body file-link provenance because the Vault
validator forbids YAML sources. The prompt contract test guards that distinction.

The documentation retains the current three memory layers and describes recall
exclusions before ranking, INHERIT.md body extraction, main-tracking Vault
checkouts with isolated PR worktrees, and Dream workspace promotion and pruning.
These corrections retire descriptions inherited from older implementations.
