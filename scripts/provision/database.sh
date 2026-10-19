#!/usr/bin/env bash
# Postgres 17 + Redis SERVER — the native data plane a gateway owns (and the CI /
# bench hosts need so the test suite can spin throwaway clusters). One source for
# install-system.sh and evaluation images. Linux: PGDG pg17 + redis-server;
# macOS: brew. Gateway data-plane processes run on POSIX hosts.
#
# Without sudo: a Linux host that already carries the whole data plane
# (pg17 binaries under /usr/lib/postgresql/17/bin, redis-server, pgbouncer)
# skips apt entirely — the keyring/apt-sources writes need root, and the
# runtime provisions its own per-cluster instance under $MEIVA_HOME/pg from a
# cached template (cli/commands/data_plane/cluster_instance.py:ensure_pg_data), so there
# is no system data dir to bootstrap. The former `--initdb` block that initdb'd
# /var/lib/postgresql/17/data (a legacy path `meiva start` never reads) is
# removed. A package manager failure is not silently swallowed: the final
# verification reports exactly what is still missing and exits non-zero, so a
# caller (`meiva start`'s host-dependency preflight) can refuse instead of
# claiming the data plane is ready.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

# The operator-approved PgBouncer build (pgdg, Ubuntu 24.04). The CI install
# action pins the same string; base/host/brew_pin.py holds the canonical value and
# tests/ci/test_pgbouncer_pin.py asserts the copies match.
PGBOUNCER_APT_VERSION="1.26.0-1.pgdg24.04+1"

# The operator-approved Redis series (8.2, as macOS pins `redis@8.2`): the newest
# patch release redis.io serves for it. base/host/brew_pin.py holds the canonical
# value; tests/ci/test_redis_pin.py asserts the copies match.
REDIS_APT_VERSION="6:8.2.*"

OS="$(prov_os)"

# apt installs pgbouncer under /usr/sbin, which is often off a non-root PATH.
pgbouncer_present() {
  command -v pgbouncer >/dev/null 2>&1 \
    || [ -x /usr/sbin/pgbouncer ] \
    || [ -x /usr/bin/pgbouncer ]
}

case "$OS" in
  linux)
    # Presence gate: skip apt when the data plane is already installed. The
    # runtime needs the server binaries only; per-cluster data dirs are
    # provisioned under $MEIVA_HOME by `meiva start`.
    if [ -x /usr/lib/postgresql/17/bin/initdb ] \
        && command -v redis-server >/dev/null 2>&1 \
        && pgbouncer_present \
        && [ -f /usr/share/postgresql/17/extension/vector.control ]; then
      prov_log "pg17 + redis + pgbouncer + pgvector already present — skipping apt (works without sudo)"
    elif ! prov_sudo apt-get update; then
      prov_log "WARNING apt-get update failed (no passwordless sudo?) — data-plane packages not installed."
      prov_log "  Re-run this script with passwordless sudo, or install the packages manually."
    else
      prov_apt_install ca-certificates curl gnupg locales

      # Redis >= 6.2 for ACL resetchannels (Ubuntu 22.04 apt ships 6.0).
      # Install from the official redis.io apt repo, held to the approved 8.2
      # series: the repo's newest release is a different major line.
      prov_apt_install gpg
      curl -fsSL https://packages.redis.io/gpg | gpg --dearmor > /tmp/redis-archive-keyring.gpg
      prov_sudo mv /tmp/redis-archive-keyring.gpg /usr/share/keyrings/redis-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(source /etc/os-release && echo $VERSION_CODENAME) main"       | prov_sudo tee /etc/apt/sources.list.d/redis.list > /dev/null
      prov_sudo apt-get update
      prov_apt_install "redis-server=${REDIS_APT_VERSION}" "redis-tools=${REDIS_APT_VERSION}"
      prov_sudo apt-mark hold redis-server redis-tools
      prov_sudo locale-gen en_US.UTF-8

      # Postgres 17 server via the official PGDG repo (Ubuntu 24.04 ships 16).
      # initdb/pg_ctl land in /usr/lib/postgresql/17/bin (not on PATH; the test
      # fixture + meiva start resolve them there). Client tools (psql/pg_dump)
      # ride along as a dependency.
      prov_apt_install postgresql-common
      prov_sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y

      # PgBouncer transaction pooler (per-cluster data plane, on by default;
      # MEIVA_PGBOUNCER_ENABLED=false is the kill-switch), pinned to the pgdg build
      # CI tests against (.github/actions/install-pg-redis) and held so an apt
      # upgrade cannot move it: its releases change pooled-session semantics.
      # Installed after the pgdg repo is added — Ubuntu's own archive ships an
      # older build. Moving the pin needs the maintainer's approval.
      prov_apt_install "pgbouncer=${PGBOUNCER_APT_VERSION}"
      prov_sudo apt-mark hold pgbouncer
      # pgvector rides the same pgdg repo — the memory search pgvector backend
      # needs the extension binaries (CREATE EXTENSION vector at connect).
      prov_apt_install postgresql-17 postgresql-17-pgvector
    fi
    ;;
  macos)
    # brew runs initdb for postgresql@17 with the current user as the bootstrap
    # superuser; meiva start provisions the per-cluster role + db on first boot
    # (cli/commands/data_plane/cluster_instance.py) and drives pg via pg_ctl, not brew
    # services.
    brew install postgresql@17 redis@8.2 pgbouncer pgvector
    ;;
esac

# Prove the result instead of trusting the branch above: `prov_sudo` degrades to
# a bare command when sudo is password-gated, and a skipped apt branch must not
# read as "installed" to its caller.
missing=""
case "$OS" in
  linux)
    [ -x /usr/lib/postgresql/17/bin/initdb ] || missing="$missing postgresql-17"
    command -v redis-server >/dev/null 2>&1 || missing="$missing redis-server"
    pgbouncer_present || missing="$missing pgbouncer"
    [ -f /usr/share/postgresql/17/extension/vector.control ] || missing="$missing pgvector"
    ;;
  macos)
    [ -x "$(brew --prefix postgresql@17)/bin/initdb" ] || missing="$missing postgresql@17"
    [ -x "$(brew --prefix redis@8.2)/bin/redis-server" ] || missing="$missing redis@8.2"
    [ -x "$(brew --prefix pgbouncer)/bin/pgbouncer" ] || missing="$missing pgbouncer"
    ;;
esac
if [ -n "$missing" ]; then
  prov_log "ERROR data plane still incomplete after provisioning:$missing"
  prov_log "  Linux: sudo apt-get install -y postgresql-17 postgresql-17-pgvector redis-server pgbouncer"
  prov_log "  macOS: brew install postgresql@17 redis@8.2 pgbouncer pgvector"
  exit 1
fi
prov_log "postgres 17 + redis + pgbouncer installed"
