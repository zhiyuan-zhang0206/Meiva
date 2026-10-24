# meiva:note
# The order a window visits its hosts. The gateway starts before every runner and stops after
# every runner: a runner registers with the gateway, so it must find one when it starts and may
# keep reporting to one until it has stopped. Preflight, checkout, verification and recovery walk
# the start order.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations


def start_order(gateway: str, runners: tuple[str, ...]) -> tuple[str, ...]:
    return (gateway, *runners)


def stop_order(gateway: str, runners: tuple[str, ...]) -> tuple[str, ...]:
    return (*runners, gateway)
