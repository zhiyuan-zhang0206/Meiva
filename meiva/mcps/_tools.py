"""The tool plumbing behind `meiva.mcps`. Private on purpose: `meiva.mcps.<name>` resolves to an MCP server
proxy, so a public submodule would shadow the server of the same name; only the `meiva/mcps` package
and its tests import it (the metering wrapper reaches it through `meiva.mcps.call_funnel`)."""

# meiva:note
# `meiva/mcps/__init__.py` is the facade (the agent-visible helpers and the module `__getattr__`); it reads the bound context once, builds a `ToolSource` of plain values from it and hands it to everything here, so a test passes a source over a real daemon socket (or a client of its own), a cache directory under `tmp_path` and the identity it wants.

from __future__ import annotations

import inspect
import json
import keyword
import types
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from base.agents.context.identity import AgentIdentity
from meiva.mcps._clients import McpClients
from meiva.mcps.config import MCPCallError, MCPConnectError, ToolInfo
from meiva.sdk_surface.pure.caller.identity_plan import attributed_agent_id, caller_facts
from meiva.security import scan_content

__all__ = [
    "DiskCache",
    "ServerProxy",
    "ToolSource",
    "cached_tools",
    "call_raw",
    "call_text",
    "list_tools",
    "make_tool_callable",
    "resolve_agent_id",
    "schema_to_signature",
]


# meiva:note
# What a server proxy and its tools run against. `remote` dials the daemon on its first request (the SDK never starts servers), `timeout_seconds` bounds each request, `cache` keeps tool metadata on disk, and `identity` is the context's agent identity: its agent id rides on every request, and a borrowed lease is revalidated at each use. A test builds the clients over the socket of a daemon it runs and the cache over a directory under `tmp_path`.
@dataclass(frozen=True)
class ToolSource:
    remote: McpClients
    timeout_seconds: float
    cache: DiskCache
    identity: AgentIdentity | None
    now: float = 0.0


# meiva:note
# The cached tool list of `document` (the parsed cache file), or None when the entry is unusable: not an object, no usable `cached_at`, older than `ttl_s` at `now`, or a `tools` field that is not a list of objects. Missing fields of a tool fill with empty values.
def cached_tools(document: object, *, now: float, ttl_s: float) -> list[ToolInfo] | None:
    data: Any = document
    if not isinstance(document, dict):
        return None
    cached_at = data.get("cached_at")
    if cached_at is None:
        return None
    try:
        age = now - cached_at
    except (TypeError, OverflowError):
        return None
    if age > ttl_s:
        return None
    tools = data.get("tools")
    items: Any = tools
    if not isinstance(tools, list):
        return None
    result: list[ToolInfo] = []
    for t in items:
        item: Any = t
        if not isinstance(t, dict):
            return None
        result.append(
            {
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "input_schema": item.get("input_schema", {}),
            }
        )
    return result


# meiva:note
# Tool metadata cache on disk: one JSON file per server under `directory`.
@dataclass(frozen=True)
class DiskCache:
    directory: Path
    ttl_s: float = 86400.0  # 24h

    def _path(self, server: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory / f"{server}.json"

    def read(self, server: str, *, now: float = 0.0) -> list[ToolInfo] | None:
        """Read disk cache for server tools. Returns None if expired or missing."""
        path = self._path(server)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return cached_tools(data, now=now, ttl_s=self.ttl_s)

    def write(self, server: str, tools: list[ToolInfo], *, now: float = 0.0) -> None:
        """Write tool list to disk cache. Best-effort: errors are silently ignored."""
        path = self._path(server)
        data = {
            "tools": tools,
            "cached_at": now,
        }
        with suppress(OSError):
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# meiva:note
# The agent id the process's work is attributed to: a borrowed lease is revalidated on each use and, while valid, takes precedence; an invalid lease raises instead of falling back to the identity's own id. None when the process has no identity or no agent. The precedence is the attribution plan's (`identity_plan.attributed_agent_id`), the same one `meiva.sdk_surface.agent_identity.agent_id` applies to a context's identity.
def resolve_agent_id(identity: AgentIdentity | None) -> int | None:
    borrowed = None if identity is None or identity.lease is None else identity.lease.validate()
    return attributed_agent_id(borrowed, caller_facts(identity))


def list_tools(source: ToolSource, server: str) -> list[ToolInfo]:
    """Load daemon metadata, using the disk cache on connection failure."""
    agent_id = resolve_agent_id(source.identity)
    try:
        tools = source.remote.list_tools(
            server, timeout_seconds=source.timeout_seconds, agent_id=agent_id
        )
    except (MCPConnectError, OSError):
        cached = source.cache.read(server, now=source.now)
        if cached is None:
            raise
        return cached
    source.cache.write(server, tools, now=source.now)
    return tools


def call_raw(source: ToolSource, server: str, tool: str, **args: object) -> dict[str, Any]:
    """Call through the daemon once; an unknown result is never replayed."""
    agent_id = resolve_agent_id(source.identity)
    return source.remote.call_tool(
        server, tool, args, timeout_seconds=source.timeout_seconds, agent_id=agent_id
    )


def call_text(source: ToolSource, server: str, tool: str, **args: object) -> str:
    """Call tool, join text content blocks into one str.

    isError=True → raise MCPCallError. An empty text join is returned as "" when
    the response is text-only (a list tool with nothing to list is a legitimate
    ""). When the join is empty AND the response also carries data a text join
    cannot represent — a non-text block (image/resource), or a structuredContent
    payload — it raises instead, pointing the caller at `.raw` so that data is
    not silently dropped. (A present structuredContent triggers this even
    alongside empty text blocks: the text is "" but the real payload is in
    structuredContent.)
    """
    raw = call_raw(source, server, tool, **args)
    text_parts = [c["text"] for c in raw["content"] if c.get("type") == "text"]
    text = "\n".join(text_parts)
    if raw["isError"]:
        raise MCPCallError(f"{server}.{tool} errored: {text or '(no message)'}")
    if not text:
        # An empty join is only an error when something a text join cannot
        # represent is present. A non-text block (image/resource) means the
        # real payload is elsewhere; a structured-only response likewise. But a
        # response whose blocks are ALL text — even if each is the empty string
        # (e.g. chrome list_pages with no open pages) — is a genuine "".
        non_text = [c for c in raw["content"] if c.get("type") != "text"]
        if non_text:
            kinds = [c.get("type", "?") for c in raw["content"]]
            raise MCPCallError(
                f"{server}.{tool} returned non-text content (kinds={kinds}); use .raw() to get the full result"
            )
        if raw["structuredContent"] is not None:
            raise MCPCallError(
                f"{server}.{tool} returned only structuredContent; use .raw() to get the full result"
            )
    # A tool result (browser page text, fetched document, remote API payload) is
    # untrusted ingested content, so it is scanned before the agent reads it.
    return scan_content(text, source=f"mcps.{server}.{tool}")


# ─── Namespace proxy ──────────────────────────────────────────────────────
#
# `meiva.mcps.<server>` goes through module-level `__getattr__`, returning
# `ServerProxy` (a `types.ModuleType` subclass so inspect.ismodule = True
# and meiva.help() walks it naturally).
#
# `meiva.mcps.<server>.<tool>` goes through ServerProxy.__getattr__,
# returning a callable (with `__name__` / `__doc__` set so
# inspect.isroutine = True and it's visible in help).


class ServerProxy(types.ModuleType):
    """Namespace for a single MCP server — `meiva.mcps.<server>`.

    Attribute access lazily returns a tool callable; `dir(proxy)` lists
    the server's tools. `__all_for_meiva__` reads disk cache first; only on cache
    miss does it connect — so `meiva.help()` doesn't trigger server startup.
    """

    def __init__(self, server: str, source: ToolSource) -> None:
        super().__init__(f"meiva.mcps.{server}")
        self._source = source
        self.__doc__ = f"Tools of MCP server {server!r}."
        self._server = server
        self._tools_cache: dict[str, ToolInfo] | None = None

    @property
    def __all_for_meiva__(self) -> list[str]:
        """`meiva.help()` checks `__all_for_meiva__` first — prefer disk cache, don't trigger connect."""
        return self._load_tool_names()

    def _load_tool_names(self) -> list[str]:
        """Get tool name list — prefer disk cache, fall back to _load_tools()."""
        if self._tools_cache is not None:
            return sorted(self._tools_cache.keys())
        cached = self._source.cache.read(self._server, now=self._source.now)
        if cached is not None:
            self._tools_cache = {t["name"]: t for t in cached}
            return sorted(self._tools_cache.keys())
        return sorted(self._load_tools().keys())

    def _load_tools(self) -> dict[str, ToolInfo]:
        if self._tools_cache is None:
            tools = list_tools(self._source, self._server)
            self._tools_cache = {t["name"]: t for t in tools}
        return self._tools_cache

    def __getattr__(self, name: str) -> Callable[..., str]:
        # ModuleType.__getattr__ is only called on ordinary lookup failure;
        # _server / _tools_cache are set in __init__ and go through instance
        # __dict__, not reaching here
        if name.startswith("_"):
            raise AttributeError(name)
        return make_tool_callable(self._source, self._server, name, self._load_tools().get(name))

    def __dir__(self) -> list[str]:
        return sorted([*self._load_tool_names(), "raw"])

    def raw(self, tool: str, **args: object) -> dict[str, Any]:
        """Call a tool and return the full result for non-text outputs: a dict
        with `content` (list of blocks), `isError`, `structuredContent`."""
        return call_raw(self._source, self._server, tool, **args)


# JSON Schema primitive `type` -> Python type, for a display-only signature.
JSON_PRIMITIVE: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def make_tool_callable(
    source: ToolSource, server: str, tool: str, info: ToolInfo | None
) -> Callable[..., str]:
    """Return a sync callable wrapping the tool; `__doc__` contains description + input_schema.

    When info=None (tool name not in cache), still returns a callable —
    the MCP server itself rejects bad names; the error info is more reliable.
    """

    def call(**kwargs: object) -> str:
        return call_text(source, server, tool, **kwargs)

    call.__name__ = tool
    call.__qualname__ = f"meiva.mcps.{server}.{tool}"
    desc = (info or {}).get("description") or ""
    schema = (info or {}).get("input_schema") or {}
    sig = schema_to_signature(schema)
    if sig is not None:
        # Display only: `help()` reads this; the call still goes through **kwargs.
        call.__signature__ = sig  # pyright: ignore[reportFunctionMemberAccess]
    parts = [desc] if desc else []
    if schema:
        parts.append(f"\nInput schema (JSON):\n{json.dumps(schema, ensure_ascii=False, indent=2)}")
    parts.append(
        f"\nFor a non-text result (image / structured) use `meiva.mcps.{server}.raw({tool!r}, ...)`."
    )
    call.__doc__ = "\n".join(parts) if parts else f"Call {server}.{tool}"
    return call


def schema_to_signature(schema: dict[str, Any]) -> inspect.Signature | None:
    """Build a display-only signature from a tool's JSON Schema, so the tool
    renders as `(*, url: str, ...)` instead of `(**kwargs)` when listed.

    Returns None (caller keeps the `(**kwargs)` signature) when the schema has no
    usable `properties`, or any property name is not a plain Python identifier.
    This does no validation and is never consulted on the call path — required vs
    optional is conveyed only by the presence or absence of a default. So a wrong
    type or a missing field still fails at the server, not here (fail-fast).
    """
    props = schema.get("properties")
    props_dict: Any = props
    if not isinstance(props, dict) or not props:
        return None
    # An MCP tool parameter name can be a non-identifier (hyphen, leading digit)
    # or a Python keyword (`from`, `class`); inspect.Parameter rejects or
    # mis-renders those. Fall back to (**kwargs) — the JSON schema in the
    # docstring still lists every parameter.
    if not all(
        isinstance(n, str) and n.isidentifier() and not keyword.iskeyword(n) for n in props_dict
    ):
        return None
    required = set(schema.get("required") or [])
    params: list[inspect.Parameter] = []
    for name, pschema in props_dict.items():
        pschema_dict: Any = pschema
        ptype: object = pschema_dict.get("type") if isinstance(pschema, dict) else None
        ann = JSON_PRIMITIVE.get(ptype, Any) if isinstance(ptype, str) else Any
        default = inspect.Parameter.empty if name in required else None
        params.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=default)
        )
    return inspect.Signature(params, return_annotation=str)
