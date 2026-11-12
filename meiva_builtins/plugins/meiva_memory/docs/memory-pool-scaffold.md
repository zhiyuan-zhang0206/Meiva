# Memory Pool Scaffold and Validator

## Responsibility

The Vault is a git checkout, with shared notes under `memory/`, which the plugin does not write to at runtime. This node
covers the host-side step that creates it and the validator that guards every
commit into it. The plugin as a whole — hooks, SDK surface, configuration — is
[meiva_memory](meiva_memory.md)'s to state.

## Host-side scaffolding

**Host-side scaffolding** (`meiva_builtins/plugins/meiva_memory/setup.py:scaffold(*, machine, facts)`, run by explicit `meiva memory init` for every enabled plugin): brings up this cluster's memory pool checkouts and lays the plugin's `template/` down inside the authoring one — `memory/MEMORY.md` (the index every agent reads) and `.githooks/pre-commit` (the character-cap guard), then arms `core.hooksPath`. Idempotent and non-destructive; never overwrites existing pool files. Kept out of `plugin.py`, which imports the agent runtime absent from the `meiva` CLI process. Converge and service startup never touch the memory repository.

## Pool validator

**`python3 validate.py`** (at the Vault root, scanning `memory/`, run by the pre-commit hook): the pool's rules in one place — UTF-8 validity, optional YAML frontmatter restricted to `description`, `verified`, and `stale_after`, checks for optional dates, rejection of legacy HTML attribution comments, inline file-link validation, value-truncation backstop, the `MEMORY.md` index's pointer targets and coverage, orphaned root notes, the character caps, fact-check probes, and the directory-structure Locality limits (20 files / 20 subdirectories per directory). Exits non-zero on any finding and runs on every commit via `.githooks/pre-commit`.

## Entry Points

- `meiva_builtins/plugins/meiva_memory/setup.py` — `scaffold`, the host-side pool bring-up
- `meiva_builtins/plugins/meiva_memory/template/validate.py` — the pool validator (`validate_bundle` checks a whole pool directory and `main` prints its findings; `validate_note` is the single-note check)

Parent: [meiva_memory](meiva_memory.md).


## Provisioning targets and configuration stages

The public plugin ABI is `scaffold(*, machine, facts)`: the explicit `meiva memory init` command reads the
Meiva home, the two memory provisioning settings (`MEIVA_MEMORY_KEEP_LOCAL`, `MEIVA_MEMORY_REMOTE`) and the
gateway access once from its configuration owner (`base.packages.plugins.loading.scaffold.ScaffoldFacts`) and hands
that frozen value to every enabled plugin's scaffold; a scaffold written for `scaffold(*, machine)` is refused
before it runs. The scaffold reads no settings of its own and has no root. It derives the authoring and
consolidated paths from `facts.home` through the foundation path helpers, and every later initialization,
template and hook operation uses those targets; the next invocation of the command reads the facts again.

The outer checkout probe retains its logging and Git error-classification
boundary. The actual initialization stage probes that same target again; an
intervening filesystem change is not hidden behind a cached boolean. This is not
an initialization mutex or a concurrency transaction.

Existing and local-only checkouts do not need a remote or gateway access. A fresh remote operation uses
`facts.memory_remote` only when needed; a split runner without it then installs a bundle from
`facts.gateway`. Both stages (authoring and, on a gateway-capable unit, the consolidated checkout) provision
from the same facts, so a setting changed while the first stage runs does not reach the second.

## Vault and optional Git LFS

Authoring clones live at `$MEIVA_HOME/vault`; the gateway clone lives at
`$MEIVA_HOME/gateway/vault`. Git hooks, remotes, and LFS configuration belong
at those repository roots. The SDK and vector index scan only the `memory/`
subtree, leaving sibling `sources/`, `papers/`, and `repos/` outside recall.

The `meiva_memory` plugin and `meiva.memory` SDK keep their names. Optional
plugin configuration `lfs_enabled` defaults to `false`; `lfs_url` defaults to
an empty string. When enabled with a nonblank URL, initialization writes a
root `.lfsconfig` with the configured LFS URL and default `.gitattributes`
tracking PDF, PNG, JPG, MP4, ZIP, and Parquet files. LFS is optional for Vaults
that leave the feature disabled.

Source references use Markdown file links in note bodies; YAML `sources` is
forbidden. Absolute link paths resolve from the Vault root, code links under
`/repos/<alias>/` resolve from the mapped checkout, and other relative paths
resolve from the note directory while staying inside Vault. The validator
requires files, rejects protocol targets, and skips links inside code. See
[the memory skill](../skills/SKILL.md#vault-paths-and-source-references).


## Reporting headroom

The pool remains the owner of note and directory caps. Its validator can export a
report-only snapshot with `--budget-report --bundle <vault> --changed <paths...>`;
changed paths are relative to that Vault. Over-limit rows remain measurements and
do not fail this mode. The ordinary validation and commit-hook gates are unchanged.
Pass the snapshot to `meizel check --budget-input <file>` to view pool and source
budgets in one table, without letting the checker discover or execute a live pool.
