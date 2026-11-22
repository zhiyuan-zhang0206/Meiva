# Structure and documentation gates

- **CI owns the merge gate.** `backend-structure` partitions its commit-stage
  hooks into lint and conditional codegen; other jobs own excluded checks,
  except the local installation warning. The selector matches the base-to-HEAD
  diff against the four hooks' `files:` regexes in `.pre-commit-config.yaml`.
  Source comment and link rules are enforced by `meizel`'s `[comments]` ratchet
  in `meizel.policy.toml`. Manual dispatch runs codegen; see the
  [runbook CI section](../docs/conventions/runbook.md#ci-continuous-integration).

Node and locked frontend dependencies are installed before structural checks,
because codegen tests and `meizel`'s TypeScript frontend use the installed
toolchain. Only the four existing generation freshness hooks are conditional;
selector failure still runs them.

The check is deterministic: no model API, semantic-review credential or review
attestation is involved. Reference and source comment rules are documented in
[Structured code comments](../docs/conventions/engineering/structured-code-comments.md).
