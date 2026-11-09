# Operator-approved tool pins

`base/host/tool_pins.py` owns the operator-approved tool pins that have no lockfile: the
uv release (version plus the four platform asset digests) and the
apt-series pins for PgBouncer and Redis. `brew_prefix` falls back to `/opt/homebrew`
when brew is absent so callers read a harmless path instead of crashing.

UV_VERSION and UV_ASSET_SHA256 are the canonical uv pin: scripts/provision/toolchain.sh and every setup-uv workflow step embed the same version, toolchain.sh embeds the same digests, and drift fails the pin contract tests.

PNPM_VERSION is the canonical pnpm pin: ui/web/package.json (`packageManager`), scripts/provision/node.sh, the macOS host-dependency remediation string and every pnpm/action-setup workflow step embed the same version, and drift fails the pin contract tests.

PGBOUNCER_APT_VERSION and REDIS_APT_VERSION are the canonical apt-series pins; scripts/provision/database.sh and the CI install steps embed the same strings and drift fails the pin tests.

Parent: [Host primitives](host.md).
