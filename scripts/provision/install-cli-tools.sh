#!/usr/bin/env bash
# Single install entry for system CLI tools — install-system.sh (and so the
# verification image, scripts/verify/Dockerfile) RUNs it.
#
# Installs: apt baseline (gnupg / ca-cert / curl) + GitHub CLI keyring +
# high-frequency CLI tools used by the agent (git / rg / jq / fd / bat /
# tree / fzf / htop / gh).
#
# Does NOT touch: nodejs / uv / Python deps / playwright / mcp-language-server
# — those are strongly tied to the host or image purpose (node.sh, toolchain.sh).
#
# Does NOT clean /var/lib/apt/lists/* — the caller decides (the verification
# Dockerfile removes them after the whole system layer).
#
# `--check` reports what the host is missing and changes nothing; converge calls it on every
# start so a wave never runs a package manager.
#
# Without sudo: a host that already carries every tool skips apt entirely
# (the gh keyring + apt-sources writes below need root), and an apt failure
# degrades to a warning instead of a hard stop — the missing tools can be
# installed later via `sudo apt-get install ...`.

set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

OS="$(prov_os)"

usage() {
    echo "usage: $(basename "$0") [--check]" >&2
    exit 2
}

CHECK_ONLY=0
[ $# -le 1 ] || usage
case "${1:-}" in
    "") ;;
    --check) CHECK_ONLY=1 ;;
    *) usage ;;
esac

# The report a host gets when a tool is missing: what is missing, and the command that installs
# it outside a start/update wave.
report_missing() {
    echo "install-cli-tools.sh: missing CLI and GNU tools: $*" >&2
    echo "  install them outside a start/update wave: bash $0" >&2
    exit 1
}

if [ "$OS" = "macos" ]; then
    missing=()
    for bin in git rg jq fd bat tree fzf htop gh; do
        command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
    done
    # coreutils is deliberately absent from this list: no code in the repository calls
    # gtimeout, gls or any other GNU name, and `brew install coreutils` is refused while
    # ca-certificates is pinned — a report that can never be satisfied is worse than a
    # convenience package a host may add on its own (2026-11-07, #6433).
    command -v gsed >/dev/null 2>&1 || [ -x /opt/homebrew/opt/gnu-sed/libexec/gnubin/sed ] || [ -x /usr/local/opt/gnu-sed/libexec/gnubin/sed ] || missing+=(gnu-sed)
    command -v ggrep >/dev/null 2>&1 || [ -x /opt/homebrew/opt/grep/libexec/gnubin/grep ] || [ -x /usr/local/opt/grep/libexec/gnubin/grep ] || missing+=(grep)
    command -v gfind >/dev/null 2>&1 || [ -x /opt/homebrew/opt/findutils/libexec/gnubin/find ] || [ -x /usr/local/opt/findutils/libexec/gnubin/find ] || missing+=(findutils)
    command -v gawk >/dev/null 2>&1 || [ -x /opt/homebrew/opt/gawk/libexec/gnubin/gawk ] || [ -x /usr/local/opt/gawk/libexec/gnubin/gawk ] || missing+=(gawk)
    command -v gtar >/dev/null 2>&1 || [ -x /opt/homebrew/opt/gnu-tar/libexec/gnubin/tar ] || [ -x /usr/local/opt/gnu-tar/libexec/gnubin/tar ] || missing+=(gnu-tar)
    [ -x /opt/homebrew/bin/bash ] || [ -x /usr/local/bin/bash ] || missing+=(bash)

    if [ ${#missing[@]} -eq 0 ]; then
        echo "install-cli-tools.sh: all CLI and GNU tools already present"
        exit 0
    fi
    if [ "$CHECK_ONLY" = 1 ]; then
        report_missing "${missing[@]}"
    fi

    echo "install-cli-tools.sh: installing missing tools via Homebrew: ${missing[*]}"
    export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1 NONINTERACTIVE=1
    brew install git ripgrep jq fd bat tree fzf htop gh bash coreutils gnu-sed grep findutils gawk gnu-tar < /dev/null
    exit 0
fi

# The complete tool set, keyed by the binary each package provides (Debian
# renames fd-find -> fdfind and bat -> batcat). A host that already carries
# every one of them — a host without sudo, a pre-provisioned CI image —
# skips apt entirely.
TOOL_BINS=(curl gpg git rg jq fdfind batcat tree fzf htop gh)
missing=()
for bin in "${TOOL_BINS[@]}"; do
    command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
done
if [ ${#missing[@]} -eq 0 ]; then
    echo "install-cli-tools.sh: all CLI tools already present — skipping apt (works without sudo)"
    exit 0
fi
if [ "$CHECK_ONLY" = 1 ]; then
    report_missing "${missing[@]}"
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "install-cli-tools.sh: requires apt-get (Debian/Ubuntu)." >&2
    echo "  macOS: brew install git ripgrep jq fd bat tree fzf htop gh" >&2
    exit 2
fi

export DEBIAN_FRONTEND=noninteractive

if ! prov_sudo apt-get update; then
    echo "install-cli-tools.sh: WARNING apt-get update failed (no passwordless sudo?) — continuing without installing: ${missing[*]}" >&2
    echo "  Install them later with: sudo apt-get install -y ${missing[*]}" >&2
    exit 0
fi
prov_sudo apt-get install -y --no-install-recommends \
    gnupg ca-certificates curl

# GitHub CLI keyring + repo (gh is not in Debian/Ubuntu default sources).
# Only touched when gh is absent — a present gh must not force root writes.
# Best-effort: without sudo the keyring write fails, and gh
# then comes from another source or must be installed manually.
if ! command -v gh >/dev/null 2>&1; then
    if ! curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
            | gpg --dearmor 2>/dev/null \
            | prov_sudo tee /usr/share/keyrings/githubcli-archive-keyring.gpg > /dev/null; then
        echo "install-cli-tools.sh: WARNING could not add the GitHub CLI apt repo (keyring write needs root) — gh must be installed manually" >&2
    else
        echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
            | prov_sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        prov_sudo apt-get update
    fi
fi

prov_sudo apt-get install -y --no-install-recommends \
    git ripgrep jq \
    fd-find bat tree fzf htop gh
