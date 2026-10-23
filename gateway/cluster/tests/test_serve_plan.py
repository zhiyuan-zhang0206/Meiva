# meiva:note
# The launch decisions of a gateway process: three facts in, one plan out.

from __future__ import annotations

import pytest

from gateway.cluster.serve_plan import GATEWAY_FACTORY, serve_plan


@pytest.mark.parametrize(
    ("serves_gateway", "off_box", "bind_host"),
    [
        # meiva:note
        # the single-box default: loopback alone
        (True, False, "127.0.0.1"),
        # meiva:note
        # a declared reachable address: every interface, both families
        (True, True, ""),
        # meiva:note
        # a runner's local gateway is dialed only from the box
        (False, True, "127.0.0.1"),
        (False, False, "127.0.0.1"),
    ],
)
def test_the_bind_address_follows_the_role_and_the_declared_address(
    serves_gateway: bool, off_box: bool, bind_host: str
) -> None:
    plan = serve_plan(serves_gateway=serves_gateway, off_box=off_box, reload=False)
    assert plan.bind_host == bind_host
    assert plan.transport_declaration_required is (bind_host != "127.0.0.1")


@pytest.mark.parametrize("off_box", [False, True])
def test_only_a_gateway_serving_unit_needs_the_secret_and_raises_the_minimum(
    off_box: bool,
) -> None:
    serving = serve_plan(serves_gateway=True, off_box=off_box, reload=False)
    runner = serve_plan(serves_gateway=False, off_box=off_box, reload=False)
    assert (serving.require_cluster_secret, serving.raise_min_code_version) == (True, True)
    assert (runner.require_cluster_secret, runner.raise_min_code_version) == (False, False)


def test_reload_leaves_the_app_to_the_worker_and_otherwise_the_parent_builds_it() -> None:
    assert serve_plan(serves_gateway=True, off_box=False, reload=True).app_factory_path == (
        GATEWAY_FACTORY
    )
    assert serve_plan(serves_gateway=True, off_box=False, reload=False).app_factory_path is None
