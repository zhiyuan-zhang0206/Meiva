# shellcheck shell=bash
# -*- shell-script -*-
# meiva:note
# Where the production service checkout lives, in one place: `$MEIVA_HOME/source` when
# MEIVA_HOME is set, and `~/.meiva/source`. Development happens in a separate clone
# (`~/Meiva`); a worktree or a commit made in the production checkout lands in the tree
# the services run from. Sourced by `scripts/setup-worktree.sh` (refuses to create or
# bootstrap a worktree there) and by the shared commit/push hook `refuse-prod-source.sh`
# (refuses to commit or push from a worktree that was made there anyway, task #7417).
#
# prod_source_containing <physical path>
#   Prints the production checkout's physical path and returns 0 when the given path
#   is that checkout or lies under it; returns 1 otherwise. Both sides compare
#   physically, so a symlinked HOME or an alias of the checkout still matches, and a
#   sibling sharing the prefix (`~/.meiva/source-dev`) does not.
prod_source_containing() {
  local path="$1" prod_dir prod_phys
  for prod_dir in ${MEIVA_HOME:+"${MEIVA_HOME%/}/source"} ${HOME:+"${HOME%/}/.meiva/source"}; do
    [ -d "$prod_dir" ] || continue
    prod_phys="$(cd -P "$prod_dir" 2>/dev/null && pwd -P)" || continue
    case "${path%/}/" in
      "${prod_phys%/}/"*) printf '%s\n' "$prod_phys"; return 0 ;;
    esac
  done
  return 1
}
