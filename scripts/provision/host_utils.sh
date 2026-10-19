#!/usr/bin/env bash
# The host utilities `meiva start` needs beyond the data plane and Node:
# `cron` (its `crontab` binary is where every OS job registers: the daily WAL-G
# backup, log rotation, packages refresh, health probe) and `curl` (the frontend
# readiness probe). Linux: apt. macOS ships curl with the OS and uses launchd
# instead of cron, so the macOS branch only verifies.
#
# Idempotent. Exits non-zero when a tool is still missing, so the caller
# (`meiva start`'s host-dependency preflight) refuses before converge instead of
# a job registration dying halfway through it.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

OS="$(prov_os)"

has_crontab() { command -v crontab >/dev/null 2>&1; }
has_curl() { command -v curl >/dev/null 2>&1; }

# A registered crontab line only fires while the daemon runs. Debian/Ubuntu's
# postinst starts it under systemd; a container without systemd needs the
# sysvinit path. Best-effort: registration works either way, and a daemon that
# refuses to start is reported instead of failing the whole provision.
start_cron_daemon() {
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    prov_sudo systemctl enable --now cron >/dev/null 2>&1 \
      || prov_log "WARNING could not enable cron.service — registered jobs will not fire"
  elif command -v service >/dev/null 2>&1; then
    prov_sudo service cron start >/dev/null 2>&1 \
      || prov_log "WARNING could not start the cron daemon — registered jobs will not fire"
  fi
}

case "$OS" in
  linux)
    if has_crontab && has_curl; then
      prov_log "crontab and curl already present — skipping apt"
    else
      prov_sudo apt-get update \
        || prov_log "WARNING apt-get update failed (no passwordless sudo?) — installing anyway"
      prov_apt_install cron curl
      start_cron_daemon
    fi
    ;;
  macos)
    prov_log "macOS provides launchd and curl with the OS — nothing to install"
    ;;
esac

rc=0
if [ "$OS" = linux ] && ! has_crontab; then
  prov_log "ERROR crontab is not available after provisioning (the cron package provides it)"
  rc=1
fi
if ! has_curl; then
  prov_log "ERROR curl is not available after provisioning"
  rc=1
fi
if [ "$rc" != 0 ]; then
  prov_log "  Linux: sudo apt-get install -y cron curl"
  prov_log "  macOS: brew install curl"
  exit 1
fi
if [ "$OS" = linux ]; then
  prov_log "crontab + curl ready"
else
  prov_log "curl ready"
fi
