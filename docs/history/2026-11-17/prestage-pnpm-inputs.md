# Prestage pnpm inputs and diagnostics

The 04:00 rollout failed during preparation, before stopping or switching hosts.
The target introduced a Mermaid patch, while preparation archived only the pnpm
manifest and lockfile. Real offline fetch reproduced
`ERR_PNPM_PATCH_FILE_PATH_MISSING`; pnpm wrote its diagnostic to stdout, which
preparation discarded.

Preparation now selects optional workspace configuration and patch paths from
the target Git objects. Unconditional pathspecs were rejected because historical
targets lack those paths and Git archive refuses missing pathspecs. Archive and
extraction run as individually checked commands so archive failure cannot be
hidden by the last command of a pipeline. Workspace and patch changes also
trigger fetch. Fetch output is captured and printed on failure, preserving quiet
success and a nonzero failure result.

Validation uses the existing real Git/shell rig and an installed pnpm offline
comparison in private temporary directories. Production source and lifecycle
remain outside this contribution's scope.

Real pnpm 11.9.0 then exposed `ERR_PNPM_PATCH_FAILED` with the target's hoisted
linker during fetch into an empty directory. The same target objects, patch and
workspace succeed offline with `--config.node-linker=isolated`. Preparation uses
that override only for its disposable cache fetch; the target's application
installation keeps its hoisted workspace configuration.
