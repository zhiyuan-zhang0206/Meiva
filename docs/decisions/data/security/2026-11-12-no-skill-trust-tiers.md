# An installed skill is trusted: there are no trust tiers

Status: decided 2026-11-12 by the project owner ("delete it, an installed skill is trusted").

## Context

[The 2026-07-29 skill decision](../../runtime/processes/shutdown/2026-07-29-skill-trust-tiers-and-install-scan.md) added two things: an
install-time scan that refuses critical findings, and a trust tier (`builtin` / `reviewed` /
`unreviewed`) recorded on every package, promoted by `meiva skill trust`. The tier existed so a runtime
layer could refuse to pull `unreviewed` text into a context unprompted. No such layer was ever written:
`install_registry.trust_by_name()` had no caller, and skill recall never read the tier. The tier was
persisted in three places (the machine's `installed.json`, the cluster `extensions.trust` column with a
CHECK, the `Extension` rows the gateway reads and writes) and kept alive by a CLI verb, converge stamping
and a "trust only rises" upsert.

## Decision

The tier is deleted. A skill that is installed is trusted; there is one gate, the install-time scan.

Deleted: `TrustTier`, `InstalledPackage.trust`, `Extension.trust` and the trust parameter of
`registry.upsert` / `register_tree` / the adoption sweep, the "never downgrade a review" SQL, converge's
`_stamp_trust` and the builtin stamping, `meiva skill trust [--revoke]`, the `trust=` text of
`meiva skill install/register/scan`, and the contract test entry that pinned the CHECK.

Kept: the install-time scan (`base/packages/skills/scan.py`), `scanned_at` and `accepted_findings` on
the registry row (the record of what was scanned and which rules a person waived), `--accept-risk`.

## Why

A label nothing reads protects nothing and implies a review that no code relied on. Tiers also asked a
person to run a verb whose result changed no behavior. The security model is that the cluster, its
agents and its installed content trust each other ([host isolation, not sandbox](2026-07-29-security-model-host-isolation-not-sandbox.md),
[no content scan](2026-11-12-no-content-scan.md)).

## Migration (expand-contract)

The cluster database column `extensions.trust` is **not** dropped in this change. The code stops reading
and writing it (`INSERT` omits it, so the column default `'unreviewed'` fills it; `SELECT` omits it), but
a runner or gateway still on the previous build reads and writes it during a rollout, so the drop is a
later contract migration (`db/docs/migrations.md`: ship the code that stops using the object first).
`db/schema.sql` therefore still declares the column and its CHECK until that migration. Old
`installed.json` rows that carry a `"trust"` key load unchanged (the model ignores unknown keys) and lose
the key the next time the registry is written.

## Consequences

- `meiva skill trust ...` now reports an unknown subcommand.
- Content installed from outside is no longer marked as such anywhere.
- An installed skill's text may be used by any layer without a tier check; nothing did the check before.
