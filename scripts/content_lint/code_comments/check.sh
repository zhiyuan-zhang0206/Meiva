#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
if [[ "${1:-}" == changed && $# == 1 && -n "${PRE_COMMIT_FROM_REF:-}${PRE_COMMIT_TO_REF:-}" ]]; then
  set -- changed --from-ref "${PRE_COMMIT_FROM_REF:?missing from-ref}" --to-ref "${PRE_COMMIT_TO_REF:?missing to-ref}"
fi
if [[ "${1:-}" == changed ]]; then
  # The changed check parses the Git blobs it compares and scans only those paths, so the
  # whole-tree TypeScript index would be built and never read. That narrowing is why CI runs
  # `check.sh validate` on every change: it is the sweep that proves every note link in the
  # repository. Every other mode here visits the tree.
  exec .venv/bin/python -m scripts.content_lint.code_comments "$@"
fi
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
git ls-files -z | node scripts/content_lint/code_comments/typescript.mjs > "$scratch/typescript.json"
exec .venv/bin/python -m scripts.content_lint.code_comments --typescript-index "$scratch/typescript.json" "$@"
