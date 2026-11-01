"""Caller operation-key validation and stored identity shared by SDK and server boundaries."""

from __future__ import annotations

import hashlib
import json

SCOPE_HEADER = "Idempotency-Scope"
PRINCIPAL_SCOPE = "principal-v1"
_STORAGE_PREFIX = "principal-v1:"


def validate_idempotency_key(value: object) -> str:
    """Reject malformed supplied keys rather than minting another operation."""
    if not isinstance(value, str):
        raise TypeError("idempotency key must be a string")
    if not 1 <= len(value) <= 128:
        raise ValueError("idempotency key must contain 1 to 128 characters")
    return value


def keyed_headers(key: str) -> dict[str, str]:
    """The header pair every keyed request carries: the operation key and its scope."""
    return {"Idempotency-Key": validate_idempotency_key(key), SCOPE_HEADER: PRINCIPAL_SCOPE}


def stored_key(kind: str, subject: str, method: str, path: str, key: str) -> str:
    """Opaque stored identity of one operation: credential, route and caller key."""
    key = validate_idempotency_key(key)
    material = json.dumps([kind, subject, method.upper(), path, key])
    return _STORAGE_PREFIX + hashlib.sha256(material.encode()).hexdigest()


# meiva:note
# One caller's chat key, narrowed to the sender and target that chose it.
#
# `chat_message_key` is cluster-wide on purpose, so a key a caller picks must arrive
# already carrying its own namespace: two senders that both choose `nightly` are two
# messages, not one silent collision. The result is an ordinary header key — the gateway
# still derives the stored identity from it.
def scoped_chat_message_key(source: str, agent_id: int, key: str) -> str:
    key = validate_idempotency_key(key)
    material = json.dumps(["chat-message-caller", source, agent_id, key])
    return hashlib.sha256(material.encode()).hexdigest()


def chat_message_key(key: str) -> str:
    """Stored `client_message_id` of one chat message.

    A chat message id is cluster-wide: reusing it for another agent, body or
    source conflicts instead of delivering a second message. Only the cluster
    administrator reaches the chat routes, so the identity is fixed to that
    principal rather than read from a request.
    """
    return stored_key("cluster", "administrator", "POST", "chat-message", key)
