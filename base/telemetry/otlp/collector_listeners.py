from base.host.net.predicates import is_loopback_host, is_unspecified_address

LOCAL_INGRESS_HOST = "127.0.0.1"
SELF_METRICS_HOST = "localhost"


def remote_ingress_host(roles: frozenset[str] | None, *, host: str, has_token: bool) -> str | None:
    if roles is None or not roles & {"gateway", "observability-station"}:
        return None
    if is_unspecified_address(host):
        raise RuntimeError(
            "cannot expose authenticated OTLP ingress: reachable host is "
            "wildcard; set MEIVA_MACHINE_HOST to this host's exact private address"
        )
    return host if has_token and not is_loopback_host(host) else None
