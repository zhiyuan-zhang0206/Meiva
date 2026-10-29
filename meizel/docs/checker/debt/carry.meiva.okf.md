---
type: doc
title: "meizel/ — Carrying recorded debt to a moved file"
description: "How `debt carry` relocates the entries of a renamed file, a renamed patch target or a moved symbol, and what writing the manifest a moved debt needs does and does not cost."
tags:
  - locality
  - tool
---

# meizel/ — Carrying recorded debt to a moved file

A baseline names files, so a `git mv` leaves the old entries paid and the new place unrecorded.
`debt carry` follows the move and relocates the entries; it never adds one, never raises a total and
records nothing that was not already recorded.

- With no flag, it takes the files git detects as renamed since the branch point with `origin/main`
  (default; the move must be staged, and a heavily rewritten file may not be detected) and relocates
  the entries whose source or target was renamed, to the owner and key they now have. Counts merge,
  and a new violation in the moved file stays an error. Run it, then `debt prune`.
- `--rename-target OLD=NEW` rewrites a patch target that was renamed.
- `--move-symbol OLD_FILE::NAME=NEW_FILE` follows a top-level function or class that moved to another
  file, which git cannot see as a rename. It scans the symbol's lines in the old file at the base and
  in the new file, and rekeys one for one at most what the symbol cost before, costs now and has
  recorded, so what it gained on the way stays an error. The old file must no longer define the name.

## A move that lands in a directory with no manifest

A file-scoped debt is looked up under the file's own directory, so an entry recorded anywhere else is
dead on arrival. When the directory a moved debt lands in has no manifest, `carry` writes one and
prints `meizel: created <path>` for each.

What that costs, and what it does not:

- **The file has to be staged.** `check` reads tracked files only, and an untracked manifest is not
  in the universe at all. `carry` says so after the paths.
- **It changes no rule.** Where an entry sits depends only on paths: an edge debt in the lowest
  common directory of its two files (`rule.py`), a file-scoped debt in the file's own directory
  (`analysis.py`). A new child manifest therefore moves no entry and alters no parent `[uses]` or
  `[exports]` declaration.
- **The move itself can leave the emptied directory stale.** Moving the last file out of `old/`
  leaves `old -> lib is declared but no fact edge needs it`: `sync --prune` drops that declaration,
  `sync --declare` supplies the one the move created. A manifest written on the way causes neither.

Parent: [[meizel/docs/checker/debt/debt.meiva.okf.md|recorded locality debt]].
