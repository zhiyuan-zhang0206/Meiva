# Changelog fragments

A notable change carries its changelog entry as a file of its own under
`.changes/`; `CHANGELOG.md` is folded from those files at release. Do not append
to `CHANGELOG.md` in a PR.

## Why one file per entry

Every merge appends to the same `## [Unreleased]` section, so two otherwise
independent PRs collide on the same lines: 104 of the last 150 merges touched
`CHANGELOG.md`, and the 2026-10-20 UI batch had to land one change at a time
because each merge rewrote the section the next one was inserting into. The
conflict is mechanical — both sides know the rule is newest first — so the
section is generated instead of merged. A by-hand merge also has no test: a
dropped entry or a swapped order reads exactly like a correct merge, and the
`### Added` / `### Changed` blocks a few rounds of hand merging left behind (16
section headings over 110 entries, now folded) are what that looks like.

## Write a fragment

`.changes/<pr>-<slug>.md`, for example `.changes/6566-timeline-card-chrome.md`.
Use the PR number once the PR exists and its task number before that. Two
notable changes in one PR write two files (`<pr>-<slug>.md` and
`<pr>-<other-slug>.md`); changing an entry an earlier PR wrote means editing
that file, because the file name is the entry's identity.

    type: Fixed

    - The entry, wrapped as it will appear in CHANGELOG.md
      ([owner doc](ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md)).

- Line 1 names the section: `Added`, `Changed`, `Deprecated`, `Removed`,
  `Fixed` or `Security`.
- The entry starts after one blank line and is the text exactly as it must read
  in `CHANGELOG.md`. Its first line is the fragment's summary in review, which
  is why there is no separate summary field to keep in agreement with it.
- Links are written as they appear in `CHANGELOG.md`, that is from the repository
  root, not from `.changes/`.
- One entry per file, no blank line inside it: a fragment is one bullet of the
  changelog, and the fold decides the spacing between bullets.

The `check-changelog-fragments` pre-commit hook and the `doc lints` CI job hold
fragments to that shape.

## Fold at release

    .venv/bin/python scripts/content_lint/assemble_changelog.py          # print the diff
    .venv/bin/python scripts/content_lint/assemble_changelog.py --write  # rewrite, delete the fragments

A fragment is ordered by the commit that added it (its mtime only while it is
still untracked), so the newest entry lands on top of its section no matter in
which order the branches merged. Sections keep Keep a Changelog's order.
`--write` also folds sections that a hand merge duplicated into one block per
type, keeping the order each section already had, so it is a fixed point: a
second run with no fragments changes nothing.

Anything that bumps the product version runs `--write` first — the step is
stated where the bump is defined, [host versioning](engineering/host-versioning.md)
— and the release commit shows `.changes/` empty. A fragment left behind is the
one way an entry misses its release.
