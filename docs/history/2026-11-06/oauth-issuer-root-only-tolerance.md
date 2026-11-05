# OAuth issuer root-only tolerance

The Google compatibility shim formerly stripped every trailing slash before
comparing issuer identifiers. That accepted a tenant path with and without a
trailing slash, and even removed multiple slashes, although the incident that
needed compatibility involved only Google's bare origin.

The shim now delegates comparison to the MCP SDK's existing issuers_match,
whose tolerance is limited to one slash at a root origin. Identical path
issuers remain accepted; different paths and path/slash mismatches remain
distinct. Regression cases demonstrate that the former shim accepted these
mismatches while the SDK helper rejects them.

The protected-resource discovery path in locked MCP 2.2.0 still invokes a strict
validator instead of this helper, so the two validator bindings remain patched.
The token-authentication shim and all three recorded patch debts are unchanged.
