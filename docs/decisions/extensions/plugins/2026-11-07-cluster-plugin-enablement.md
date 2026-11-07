# Cluster plugin enablement

## Scope

Meiva has one authoritative gateway and several runners. A user enables a capability
for the cluster; a machine being offline must not lose that decision, and a new
home must inherit it. Plugin code continues to load at process birth. Installation,
service gates and machine capability requirements retain their existing owners.

## Prior art and alternatives

The existing `extensions` registry already owns `default_enabled`, including
policy preservation across upgrades. Skills already materialize cluster facts at
startup. Reuse these owners. A UI-only Enable all fan-out would leave offline
and future machines unchanged; a separate settings table would duplicate policy.
A hot plugin reload would require undoing process-wide registrations and is beyond
this change's startup activation contract.

## Decision

`base/packages/extensions/plugin_enable.py` projects plugin rows into the atomic
`plugin_enable_policy.json` runtime cache. The gateway initializes previously
unregistered plugins from its current choices before serving requests; existing
cluster rows win. Repo plugins carry no blob; an external plugin is registered
with its actual content. Returning runners only read the authority and never seed
it from their local settings. Missing local content is not fabricated by this
projection: the decision applies when that plugin is installed.

The loader overlays cluster decisions over legacy local entries. Local CLI and
host RPC writes refuse a managed plugin rather than creating a second authority.
The cluster API saves one decision without dialing runners. Startup materialization
refreshes it on gateway converge and agent-host boot. If the data plane is
unreachable, startup warns and retains the last cache under the existing extension
boot recovery contract; catch-up needs a successful subsequent start.

## Consequences

Settings has one switch per plugin. A successful save reports that services need
restarting; it does not claim loaded code changed immediately. MCP retains its
host capability model. Plugin service gates remain separate from agent-face
registration. Offline/new-home inheritance and refusal of local overrides are
covered by tests against the real registry and files.
