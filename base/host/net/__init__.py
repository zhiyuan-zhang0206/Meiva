from __future__ import annotations

_SCHEME = "Bearer "


def bearer_header(secret: str) -> dict[str, str]:
    return {"Authorization": f"{_SCHEME}{secret}"}


class ClusterSecretMissingError(RuntimeError):
    pass


def require_cluster_secret(secret: str, *, where: str) -> str:
    if not secret:
        raise ClusterSecretMissingError(
            f"{where} has no MEIVA_CLUSTER_SECRET, and the cluster API always authenticates. "
            "Set it in the gateway home's .env to a fresh URL-safe token "
            "(python3 -c 'import secrets; print(secrets.token_urlsafe(32))'), then run `meiva start --all-services` (or this unit's own service selection); "
            "`meiva init` mints one for every new home"
        )
    return secret
