#!/usr/bin/env bash
# Node for the frontend (Next.js) build + agent tooling — one source for
# host preparation / the verification image / CI. Linux: nodesource 22 (Ubuntu 24.04 ships 18).
# macOS: the linked `node` formula (npx must land on PATH for meiva-browser);
# the keg-only `node@22` is the force-linked fallback. Idempotent.
#
# Without sudo: a host that already runs node >= 20.9 (the frontend's floor)
# skips apt entirely — the nodesource setup + apt install need root. A failed
# install is not silently swallowed: the final check exits non-zero so a caller
# (`meiva start`'s host-dependency preflight) can refuse instead of letting the
# frontend session die on a missing `npm`.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

OS="$(prov_os)"

# `node` >= 20.9 plus the npm/npx pair the frontend and meiva-browser execute.
node_toolchain_ok() {
  command -v node >/dev/null 2>&1 || return 1
  command -v npm >/dev/null 2>&1 || return 1
  command -v npx >/dev/null 2>&1 || return 1
  local major minor
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  minor="$(node -p 'process.versions.node.split(".")[1]' 2>/dev/null || echo 0)"
  [ "$major" -gt 20 ] 2>/dev/null \
    || { [ "$major" -eq 20 ] 2>/dev/null && [ "$minor" -ge 9 ] 2>/dev/null; }
}

case "$OS" in
  linux)
    if node_toolchain_ok; then
      prov_log "node $(node --version) already present — skipping apt (works without sudo)"
    elif ! prov_sudo apt-get update; then
      prov_log "WARNING apt-get update failed (no passwordless sudo?) — node not installed; the frontend needs it (run the gateway headless without it)"
    else
      prov_apt_install ca-certificates curl gnupg
      curl -fsSL https://deb.nodesource.com/setup_22.x | prov_sudo bash -
      prov_apt_install nodejs
    fi
    ;;
  macos)
    # brew's bin is not on a non-login PATH; the frontend supervisor prepends it
    # too, and this script needs the same view to verify what it installed.
    PATH="$(brew --prefix)/bin:$PATH"
    export PATH
    # The unversioned `node` formula is linked into /opt/homebrew, so npx lands
    # on PATH; `node@22` is keg-only (never symlinked) and would leave npx
    # unreachable for the meiva-browser daemon. Prefer the linked formula, and
    # force-link the keg on the fallback path (QA review 2026-09-01, PR #1286 P1).
    brew install node 2>/dev/null \
        || { brew install node@22 2>/dev/null && brew link --force node@22; }
    ;;
esac
if ! node_toolchain_ok; then
  prov_log "ERROR Node.js >= 20.9 with npm/npx is not available after provisioning (got: $(node --version 2>/dev/null || echo 'not on PATH'))"
  prov_log "  Linux: curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - && sudo apt-get install -y nodejs"
  prov_log "  macOS: brew install node"
  exit 1
fi
prov_log "node $(node --version)"
