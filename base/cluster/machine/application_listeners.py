def gateway_bind_host(*, serves_gateway: bool, off_box: bool) -> str:
    return "" if serves_gateway and off_box else "127.0.0.1"


def ops_address(*, off_box: bool) -> str:
    return "0.0.0.0" if off_box else "127.0.0.1"  # noqa: S104
