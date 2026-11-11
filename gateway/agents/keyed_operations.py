# meiva:note
# The keyed-operation admission dependency and the immutable operation receipts shared by the
# question, publication and reply-suggestion routes.

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from fastapi import Header, HTTPException, Request
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from base.host.boundary.json_values import is_object_dict
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    PrincipalScopeError,
    principal_key,
    request_key,
)


# meiva:note
# A keyed operation's stored request and receipt, both `jsonb`.
@dataclass(frozen=True)
class _StoredReceipt:
    request: object
    receipt: object


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
            status_code=422, detail="keyed operations require verified principal-v1 scope"
        )
    try:
        return principal_key(principal, request.method, request.url.path, idempotency_key)
    except (PrincipalScopeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# meiva:note
# Use the same credential scope as other gateway keyed operations.
def keyed_request_key(request: Request, key: str) -> str:
    try:
        return request_key(request, key, method="POST", path=request.url.path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# meiva:note
# Serialize a notice operation and reject reuse with changed inputs.
def existing_receipt(
    conn: psycopg.Connection,
    path: str,
    key: str,
    body: dict[str, object],
) -> dict[str, object] | None:
    with conn.cursor(row_factory=class_row(_StoredReceipt)) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (path + ":" + key,))
        cur.execute(
            "SELECT request, receipt FROM keyed_operation_receipts "
            "WHERE path = %s AND operation_key = %s",
            (path, key),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if row.request != body:
        raise HTTPException(
            status_code=409, detail="idempotency key identifies a different keyed operation"
        )
    if not is_object_dict(row.receipt):
        raise TypeError("stored keyed operation receipt is not an object")
    return row.receipt


# meiva:note
# Persist an immutable result in the transaction containing its effects.
def save_receipt(
    conn: psycopg.Connection,
    path: str,
    key: str,
    body: dict[str, object],
    receipt: dict[str, object],
) -> None:
    conn.execute(
        "INSERT INTO keyed_operation_receipts (path, operation_key, request, receipt) "
        "VALUES (%s, %s, %s, %s)",
        (path, key, Jsonb(body), Jsonb(receipt)),
    )
