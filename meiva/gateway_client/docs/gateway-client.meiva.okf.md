---
type: doc
title: Gateway Client Request Plans
description: How meiva.gateway_client builds each gateway request as a value and where it is tested.
tags: [sdk, gateway, testing]
---

# Gateway Client Request Plans

`meiva.gateway_client` is the SDK's typed way to call the gateway. Each public
function is a thin facade: it reads the process once (the bound context, the
caller's actor, a fresh idempotency key), asks a plan module for the request,
and runs that plan through the base transport (`_run` in
`meiva/gateway_client/__init__.py`). The base transport owns HTTP, retries and
wire-error translation ([[base/agents/context/docs/clients.meiva.okf.md]]).

## Plans

A plan is a frozen `Request` (`meiva/gateway_client/plans.py:Request`): method,
path, body or query, an optional timeout, an optional idempotency key and an
optional retry count. A missing timeout or retry count leaves the context's own
configured value in charge. Two modules build plans and are declared `strict`:

- `meiva/gateway_client/plans.py` — spawn (and the receipt that reports a
  withdrawn model), messages, system notes, directory pages, label changes.
- `meiva/gateway_client/agent_plans.py` — terminate, restart, resurrect, page
  registration and the page-close decision: a page is closed only by the
  registry row that was observed, so `open_page` selects it and the plan names its id.

Anything a plan needs from the process arrives as a value: the actor string,
the minted key, the validated arguments. A key supplied by the caller is
validated when the plan is built, so an invalid key fails before any HTTP.

## Tests

Plan tests pass values and assert the returned `Request`
(`meiva/gateway_client/tests/test_plans.py`). Facade and transport tests run over
a real port-0 listener with a recording transport in front of it
(`meiva/tests/fixtures/gateway_wire.py`): the test asserts the requests
the SDK sent, what the listener received and the result the SDK returned or
raised. A transport error is scripted either before the request leaves (the
pre-send family) or after the listener really received it (the reply is lost), so
retries under one key are observed for real. No test replaces the client or the
transport with a stand-in.
