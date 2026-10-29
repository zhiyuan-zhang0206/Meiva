# Configured provider declarations

Date: 2026-10-29

## Context

SystemOne services share one typed wire protocol. The existing `lm_typesafe`
plugin owns hosted Jev; optional local services need operator-owned endpoint and
model registrations in that same protocol boundary.
The catalog composition root already takes an explicit home and owns discovery,
installation and publication. Static provider faces use `contribute()` with no
arguments. Reading an ambient home or Settings from a local provider would let
`build_model_catalog(home=one)` accidentally use another home's configuration.

## Decision

A provider may declare `configure(config)` instead of `contribute()`. The existing
catalog loader reads its declared plugin config schema and that home's image,
composes the normal cluster bootstrap policy, and passes the resolved value to
the configured declaration. Both entry points at once are ambiguous and fail.
Missing schemas, malformed images and contribution failures leave no published
partial catalog. Providers snapshot inputs into their returned bindings.

## Consequences

The TypeSafe face returns its unchanged Jev declaration alongside optional local
registrations. Its `local_endpoint` and `local_models` fields cannot repoint Jev.
Other static providers keep their zero-argument declarations and signatures.
The wire codec stays in `lm_typesafe`, shared by both bindings, rather than
entering the generic typed decision contract or its extension API.

Configuration uses the existing plugin image/bootstrap system. Catalogs freeze
resolved inputs; edits require rebuilding each owner. Enrolled runners still
require a matching gateway projection. Inference service provisioning, artifact
acceptance and deployment remain separate from catalog construction. The core
adds one configuration-injection seam rather than a runtime model framework.
