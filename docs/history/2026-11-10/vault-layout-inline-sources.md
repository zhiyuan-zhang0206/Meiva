# Vault layout and inline evidence links

The Vault Git checkout owns shared memory notes under `memory/` and evidence
assets in sibling directories. Keeping the repository boundary separate from
the note boundary allows Git hooks and optional LFS configuration to apply to
all evidence while search indexes only notes. The plugin and SDK retain their
`meiva_memory` and `meiva.memory` names.

Evidence references live at the claim in Markdown body links. YAML `sources`
is rejected so provenance has one representation. Vault-absolute paths identify
assets independently of the author's machine; code repository aliases provide
explicit mappings when code lives outside the Vault. Relative links retain the
standard meaning of being relative to the referring note.

Legacy checkouts require an operator-controlled migration. Initialization must
preserve existing dirty files, unpushed commits and repository configuration;
production activation and index rebuilding remain separate from this code change.

CI review required smaller orchestration functions. Shared root-note rejection
and source-repository mapping were extracted into focused helpers, preserving
the initialization and resolution behavior while staying within complexity
budgets.
