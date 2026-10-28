# meiva:note
# The digest form a bearer token is compared in.
#
# SHA-256 hex: the accepting side keeps and compares digests, never a token in clear, and the
# comparison is constant-time (`hmac.compare_digest` at the call site). Pure — hashlib only — so the
# admission decisions import it without dragging the store with them.
# meiva:link base/host/db_authority/docs/api-tokens.meiva.okf.md

from __future__ import annotations

import hashlib

__all__ = ["token_digest"]


# meiva:note
# SHA-256 of a bearer token: the form an accepting side keeps and compares.
def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
