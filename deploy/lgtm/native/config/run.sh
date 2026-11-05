#!/usr/bin/env bash
set -euo pipefail

if [[ -f "{{MEIVA_HOME}}/lgtm/native/grafana/admin_password" ]]; then
    export GRAFANA_ADMIN_PASSWORD="$(<"{{MEIVA_HOME}}/lgtm/native/grafana/admin_password")"
fi
set -a
if [ -f "{{REPO}}/deploy/lgtm/.env" ]; then
    . "{{REPO}}/deploy/lgtm/.env"
fi
export GRAFANA_ROOT_URL="${GRAFANA_ROOT_URL:-http://localhost:{{LGTM_GRAFANA_PORT}}}"
. "{{MEIVA_HOME}}/lgtm/native/config/runtime.env"
set +a

exec "{{MEIVA_HOME}}/lgtm/native/grafana-home/bin/grafana" server \
    --config "{{MEIVA_HOME}}/lgtm/native/config/grafana.ini" \
    --homepath "{{MEIVA_HOME}}/lgtm/native/grafana-home"
