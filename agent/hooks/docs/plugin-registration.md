# Plugin and hook registration

`agent/hooks/__init__.py` is the public face of the graph-edge hook layer: it
re-exports `Hook`, `HookName` and `make_hook_runner` from `_registry`, and the
`BUILTIN_PLUGINS` constant beside them.

Parent: [Hooks](hooks.md).
