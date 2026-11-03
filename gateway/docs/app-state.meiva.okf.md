---
type: doc
title: "Gateway App State"
description: "The typed shape of app.state, how a handler reaches it, and where a directory declares the attributes it owns."
tags:
- gateway
- app-state
---

# Gateway app state

Starlette's `State` answers any attribute with `Any`, so `request.app.state.x` carries no type.
`GatewayAppState` (`gateway/http/config.py`) declares the attributes the app root assigns, by
name and type and never with a value: `State` keeps its attributes in a dict, so a class
attribute would shadow what the root assigns. `create_app` gives the app such a state, a handler
reads it with `gateway_state(request)` (or `GatewayStateDep` as a parameter), and a test that
assigns `client.app.state.bus = fake` still reaches the handlers because every read is fresh.
An attribute whose type one directory owns is declared by that directory on a subclass of
`GatewayAppState`, reached with `state_as(request, ThatSubclass)` (the model catalog is the first:
`CatalogAppState` in `gateway/http/model_catalog.py`, read with `request_catalog`); the root's state
class, `GatewayServerState` in `gateway/app.py`, inherits every subclass. `app_state_as` is the one place the untyped `app` is narrowed, and a
state of another class raises `TypeError`, so an app built without the root fails at the first
read. A test that builds a bare `FastAPI()` for a router gives it the state class that router reads first
(`GatewayAppState()`, or the subclass it reaches).

Parent node: [[gateway.meiva.okf.md|Gateway]].
