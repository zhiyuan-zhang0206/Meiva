# Effect shells inside the debt ratchet

`docs/conventions/architecture/code-placement-and-purity.md` places loops, handlers and I/O
adapters in the effect-shell tier: default purity (`"none"`), or `"strict-to-be"` while the
directory is migrating. The two pieces of that sentence pull apart in practice:

- A shell declared `"none"` (or left to the default) is not scanned. Splitting a decision out of
  a mixed module moves the file reads into a shell the ratchet cannot see, so `meizel debt diff`
  reports the moved calls as repaid.
- A shell kept `"strict-to-be"` keeps those calls as recorded debt that no edit to the shell can
  clear, because the shell exists to make the call.

The open question is the terminal representation of an effect shell: a purity tier or a per-file
declaration that the scanner reads (so the shell stays inside the ratchet and its allowed
effects are written down), or an explicit decision that shells are outside it. Until that is
decided, a split that creates a shell does not mark it `"none"` to clear debt.

Shells that carry the question today:

- `base/lm/attach/files.py` (declared `"none"`): `Path.resolve`, `Path.stat` and
  `Path.read_bytes`, moved out of `packing.py` by `2ffb0566f`.
- `base/lm/pricing/archive.py` (no declaration, so the default): `pkgutil.get_data`, moved out of
  the pricing module by `c1f30d2b5`.
- The transports and sessions under `base/lm/harness/`, whose `async def` bodies are recorded
  as `impure-call` debt in their directories' `meizel.toml`.
