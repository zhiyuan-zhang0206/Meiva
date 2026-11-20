#!/usr/bin/env bash
# meiva:note
# Defense in depth behind `setup-worktree.sh`'s refusal (task #7417): a worktree made
# with a bare `git -C ~/.meiva/source worktree add` skips that script, but it shares the
# production checkout's installed hooks, so the commit or push is where it can still be
# stopped. Registered on the pre-commit and pre-push stages only — never on
# post-checkout or anything else `git worktree add` / `checkout` triggers, because the
# fleet update preflight (`fleet_pre_root_check.sh`, `fleet_pre_verify.sh`) makes a
# legitimate temporary read-only worktree of the production checkout.
set -euo pipefail

# shellcheck source=scripts/hooks/prod-source.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prod-source.sh"

common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
main_root="$(cd -P "$(dirname "$common_dir")" && pwd -P)"
top="$(cd -P "$(git rev-parse --show-toplevel)" && pwd -P)"
for checked in "$main_root" "$top"; do
  if prod_phys="$(prod_source_containing "$checked")"; then
    echo "✖ refusing to commit or push from the production source checkout ($checked is in $prod_phys)." >&2
    echo "  Work in your development clone instead: cd ~/Meiva && bash scripts/setup-worktree.sh <task>" >&2
    exit 1
  fi
done
