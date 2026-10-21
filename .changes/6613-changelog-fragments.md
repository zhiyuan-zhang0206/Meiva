type: Changed

- The changelog is folded instead of merged: a notable change writes one file per
  entry under `.changes/`, and `scripts/content_lint/assemble_changelog.py --write`
  folds them into `CHANGELOG.md` at release, newest first by the commit that added
  the fragment. Every merge appended to the same `## [Unreleased]` section — 104
  of the last 150 merges touched the file — so independent PRs collided on it and
  the 2026-10-20 UI batch had to land one change at a time; the fold is also the
  first thing that can prove an entry survived a merge, and it collapses the
  section headings hand merges duplicated (16 over 110 entries)
  ([changelog fragments](docs/conventions/changelog.md)).
