# meiva:note
# The keyed-operation admission dependency shared by the notice and delivery routes.

from fastapi import Header, HTTPException, Request

from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    PrincipalScopeError,
    principal_key,
)


def operation_key(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> str:
    # meiva:note
    # Require a verified credential and explicit principal-v1 identity.
    principal = getattr(request.state, "auth_principal", None)
    if idempotency_scope != PRINCIPAL_SCOPE or not isinstance(principal, AuthPrincipal):
        raise HTTPException(
            status_code=422, detail="guarded notices require verified principal-v1 scope"
        )
    try:
        return principal_key(principal, request.method, request.url.path, idempotency_key)
    except (PrincipalScopeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
