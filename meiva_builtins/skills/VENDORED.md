# Vendored skills

Skills under `meiva_builtins/skills/` that were **copied from an upstream source
and adapted**, rather than authored here. One row per vendored skill.

Vendoring is the settled posture for external capability packs: an unadapted
install carries machinery specific to whatever other tool it was written for
(cross-references by that tool's skill namespace, assumptions about that
tool's native features) that does not hold in Meiva, so we copy, strip, rewire
to `meiva.*` idioms, and own the sync cost.

Anything landed here is synced by converge
(`cli/commands/extensions/skills_sync.py`) and bypasses the install-time supply-chain
scanner — so a vendored pack must be **read in full before it is committed**,
not merely copied.

## The license bar (2026-08-14)

A vendored pack ships from this repo under Apache-2.0, which means the row below
has to name a **license grant that actually covers the copied files** — a
published `LICENSE` in the upstream repo the files came from, or an explicit
grant in the files themselves.

"A similarly named skill elsewhere is Apache-2.0" is not that. It was the basis
recorded for `meiva-ui/design` + `meiva-ui/dataviz`, vendored 2026-07-30 out of the
**closed-source** Claude Code CLI binary (`@anthropic-ai/claude-code` 2.1.220):
the row pointed at `frontend-design` in
[`anthropics/skills`](https://github.com/anthropics/skills), which does ship an
Apache-2.0 `LICENSE.txt` — but that repo carries no `dataviz` and no
`artifact-design`, so the license of a neighbouring skill was never evidence
about the copied files. Both skills were removed from this repo on 2026-08-14.

Content extracted from a closed-source distribution does not become
redistributable by resembling something that is. Vendor from a source whose
license you can point at.
