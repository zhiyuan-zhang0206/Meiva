# meiva:note
# Converge step: the gateway home keeps its OTLP telemetry token.
#
# The token is the cluster's OTLP relay ingress bearer (`base.host.db_authority.api`). It is
# minted once, kept in the home's private authority store and independent of the human secret, so
# every reader of it (the collector config this converge renders next, `issue-unit`, the
# heartbeat's station probe) finds it published or none was ever minted. The step sits before the
# collector step because that step renders the token into the sidecar's config: a first start that
# published it later would render a collector with no remote ingress.

from __future__ import annotations

from base.host.db_authority.api import ensure_telemetry_token
from base.host.db_authority.ledger import new_secret_token
from cli.commands.converge.spec import ConvergeCtx


# meiva:note
# Publish a new random token unless the home already holds one. The draw is here, at the effect
# shell, and the owner of the file takes it as a value.
def ensure_telemetry_token_step(ctx: ConvergeCtx) -> None:
    ensure_telemetry_token(ctx.meiva_home.resolve(), new_secret_token())
