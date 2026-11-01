from __future__ import annotations

from dataclasses import dataclass

from meizel.allowances import Policy
from meizel.graph.model import MeizelError

DEFAULT_TS_IMPURE_CALLS: frozenset[str] = frozenset(
    {
        "child_process",
        "dgram",
        "dns",
        "dns/promises",
        "fs",
        "fs/promises",
        "http",
        "http2",
        "https",
        "net",
        "readline",
        "Date.now",
        "Math.random",
        "PerformanceObserver",
        "BroadcastChannel",
        "EventSource",
        "IntersectionObserver",
        "MutationObserver",
        "Notification",
        "ResizeObserver",
        "SharedWorker",
        "WebSocket",
        "Worker",
        "XMLHttpRequest",
        "alert",
        "cancelAnimationFrame",
        "clearInterval",
        "clearTimeout",
        "confirm",
        "console.debug",
        "console.error",
        "console.info",
        "console.log",
        "console.warn",
        "crypto.getRandomValues",
        "crypto.randomUUID",
        "document",
        "fetch",
        "history",
        "localStorage",
        "location",
        "navigator",
        "performance.now",
        "process.env",
        "prompt",
        "queueMicrotask",
        "requestAnimationFrame",
        "sessionStorage",
        "setInterval",
        "setTimeout",
        "window",
    }
)

DEFAULT_TS_IMPURE_HOOKS: frozenset[str] = frozenset(
    {
        "createContext",
        "useActionState",
        "useContext",
        "useDeferredValue",
        "useEffect",
        "useId",
        "useInfiniteQuery",
        "useInsertionEffect",
        "useLayoutEffect",
        "useMutation",
        "useOptimistic",
        "useQuery",
        "useQueryClient",
        "useReducer",
        "useRef",
        "useState",
        "useSuspenseQuery",
        "useSyncExternalStore",
        "useTransition",
    }
)


# meiva:note
# Next.js replaces `process.env.NEXT_PUBLIC_<NAME>` with the value the build saw, in every file it
# bundles, as long as that variable is set when it builds. There the expression is a constant of the
# build, not a read of the environment. A file outside these roots (a config, a script, a test) is not
# bundled and reads it.
INLINED_ENV_PREFIX = "NEXT_PUBLIC_"
INLINED_ENV_ROOTS: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TsConfig:
    scopes: tuple[str, ...]
    import_sources: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...]
    extensions: tuple[str, ...]
    impure_calls: frozenset[str]
    impure_hooks: frozenset[str]
    max_public_symbols: int
    max_params: int
    max_attr_hops: int
    inlined_env_prefix: str
    inlined_env_roots: tuple[str, ...]


def _policy_int(policy: Policy, section: str, default: int) -> int:
    raw = policy.names("purity", section)
    if not raw:
        return default
    if len(raw) != 1 or not raw[0].isdigit() or int(raw[0]) < 1:
        raise MeizelError(f"purity.{section} must contain one positive integer")
    return int(raw[0])


def _parse_aliases(entries: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in entries:
        prefix, sep, target = item.partition("=")
        prefix, target = prefix.strip(), target.strip()
        if not sep or not prefix or not target or prefix.count("*") > 1 or target.count("*") > 1:
            raise MeizelError(f"invalid TypeScript alias {item!r}: expected PREFIX=TARGET")
        pairs.append((prefix, target))
    return tuple(pairs)


def load_ts_config(policy: Policy) -> TsConfig:
    calls = frozenset(policy.names("typescript", "impure-calls")) or DEFAULT_TS_IMPURE_CALLS
    hooks = frozenset(policy.names("typescript", "impure-hooks")) or DEFAULT_TS_IMPURE_HOOKS
    exts = policy.names("typescript", "extensions") or (".ts", ".tsx", ".mts", ".cts")
    return TsConfig(
        scopes=policy.names("typescript", "scopes"),
        import_sources=policy.names("typescript", "import-sources"),
        aliases=_parse_aliases(policy.names("typescript", "aliases")),
        extensions=exts,
        impure_calls=calls,
        impure_hooks=hooks,
        max_public_symbols=_policy_int(policy, "max-public-symbols", 6),
        max_params=_policy_int(policy, "max-params", 6),
        max_attr_hops=_policy_int(policy, "max-attr-hops", 3),
        inlined_env_prefix=INLINED_ENV_PREFIX,
        inlined_env_roots=policy.names("typescript", "inlined-env-roots") or INLINED_ENV_ROOTS,
    )
