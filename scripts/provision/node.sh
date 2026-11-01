#!/usr/bin/env bash
# Node + pnpm for the frontend (Next.js) build + agent tooling — one source for
# host preparation / the verification image / CI. Linux: nodesource 22 (Ubuntu 24.04 ships 18)
# plus global pnpm at the canonical pin. macOS: the linked `node` formula (npx must land on
# PATH for meiva-browser; `node@22` is the force-linked fallback) plus global pnpm. Idempotent.
#
# Without sudo: a host that already runs node >= 22.13 (pnpm 11's floor) and the pinned pnpm
# skips apt/npm entirely. A failed install is not silently swallowed: the final check exits
# non-zero so a caller (`meiva start`'s host-dependency preflight) can refuse instead of
# letting the frontend session die on a missing binary.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

# Canonical pin lives in base/host/brew_pin.py; contract-tested in test_toolchain_uv_pin.py.
PNPM_VERSION="11.9.0"

OS="$(prov_os)"

# `node` >= 22.13 (pnpm 11's engine floor) plus the npm/npx pair that ships with Node.
node_runtime_ok() {
  command -v node >/dev/null 2>&1 || return 1
  command -v npm >/dev/null 2>&1 || return 1
  command -v npx >/dev/null 2>&1 || return 1
  local major minor
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  minor="$(node -p 'process.versions.node.split(".")[1]' 2>/dev/null || echo 0)"
  [ "$major" -gt 22 ] 2>/dev/null \
    || { [ "$major" -eq 22 ] 2>/dev/null && [ "$minor" -ge 13 ] 2>/dev/null; }
}

pnpm_ok() {
  command -v pnpm >/dev/null 2>&1 || return 1
  [ "$(pnpm --version 2>/dev/null || true)" = "$PNPM_VERSION" ]
}

install_pnpm() {
  local prefix
  prefix="$(npm config get prefix 2>/dev/null || true)"
  if [ -n "$prefix" ] && [ -w "$prefix" ] && { [ ! -e "$prefix/bin" ] || [ -w "$prefix/bin" ]; }; then
    npm install -g "pnpm@${PNPM_VERSION}"
  else
    prov_sudo npm install -g "pnpm@${PNPM_VERSION}"
  fi
}

case "$OS" in
  linux)
    if node_runtime_ok; then
      prov_log "node $(node --version) already present — skipping apt (works without sudo)"
    elif ! prov_sudo apt-get update; then
      prov_log "WARNING apt-get update failed (no passwordless sudo?) — node not installed; the frontend needs it (run the gateway headless without it)"
    else
      prov_apt_install ca-certificates curl gnupg
      curl -fsSL https://deb.nodesource.com/setup_22.x | prov_sudo bash -
      prov_apt_install nodejs
    fi
    if node_runtime_ok && ! pnpm_ok; then
      install_pnpm || prov_log "WARNING pnpm@${PNPM_VERSION} install failed"
    fi
    ;;
  macos)
    # brew's bin is not on a non-login PATH; the frontend supervisor prepends it
    # too, and this script needs the same view to verify what it installed.
    PATH="$(brew --prefix)/bin:$PATH"
    export PATH
    if ! node_runtime_ok; then
      # The unversioned `node` formula is linked into /opt/homebrew, so npx lands
      # on PATH; `node@22` is keg-only (never symlinked) and would leave npx
      # unreachable for the meiva-browser daemon. Prefer the linked formula, and
      # force-link the keg on the fallback path (QA review 2026-09-01, PR #1286 P1).
      brew install node 2>/dev/null \
          || { brew install node@22 2>/dev/null && brew link --force node@22; }
    fi
    if node_runtime_ok && ! pnpm_ok; then
      install_pnpm || prov_log "WARNING pnpm@${PNPM_VERSION} install failed"
    fi
    ;;
esac
if ! node_runtime_ok || ! pnpm_ok; then
  prov_log "ERROR Node.js >= 22.13 with npm/npx and pnpm ${PNPM_VERSION} is not available after provisioning (node: $(node --version 2>/dev/null || echo 'not on PATH'), pnpm: $(pnpm --version 2>/dev/null || echo 'not on PATH'))"
  prov_log "  Linux: curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - && sudo apt-get install -y nodejs && sudo npm install -g pnpm@${PNPM_VERSION}"
  prov_log "  macOS: brew install node && npm install -g pnpm@${PNPM_VERSION}"
  exit 1
fi
prov_log "node $(node --version), pnpm $(pnpm --version)"
